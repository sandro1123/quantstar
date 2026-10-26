"""issues 命令组 — 每日问题报告 (只读)

设计稿: 分层讨论/每日问题报告设计稿-A-20260828.md
只读三重保障 (复用 advisory 模式):
  ① sqlite mode=ro URI 物理只读 (写操作抛 OperationalError)
  ② 不 bootstrap (不初始化 delivery/alert/execution 层)
  ③ 不联网 (全走本地 qs.db + 本地日志文件)
输出契约: --json 稳定 schema (schema_version=1, 键英文值可中文, *_zh 成品中文);
人类可读输出全中文。失败诚实: ok:false + error.code + 非零 exit, 绝不返回假值。

收集器注册表: 每个问题源一个 collector(conn, date) -> (issues, asof)。
批 1 = 骨架 + S1/S2; 批 2 = S3~S11 全接入 + stale-alerts 子命令 (旧账清单);
批 3 = 降噪三闸 (S3 聚合 top-N / P0-3 去重口径 / 已知闸)。
exit code: 0=成功(含有问题) / 1=聚合本身出错 / 2=参数错。
有问题不用非零 exit — quantmaster 定时任务须区分"命令坏了"和"系统有问题"。
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import click

SCHEMA_VERSION = 1

# ── 基础设施 (与 advisory 同构, 独立拷贝不跨模块 import — 两组命令各自只读自治) ──

def _resolve_db_path() -> str:
    """库路径解析 — 与 common.database.Database 默认逻辑一致
    (QS_TEST_DB 测试隔离变量优先, 生产从不设置)。"""
    return os.environ.get("QS_TEST_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "qs.db")

def _resolve_logs_dir() -> str:
    """日志目录 — QS_TEST_LOGS 测试隔离变量优先, 默认 repo/logs。"""
    return os.environ.get("QS_TEST_LOGS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "logs")

def _today() -> str:
    """当前北京日期 (YYYY-MM-DD) — 独立函数便于测试注入锁死, 防日期漂移。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def _ro_connect() -> sqlite3.Connection:
    """只读连接 (保障①): mode=ro 物理杜绝写; timeout 2s 防与生产写撞锁。"""
    conn = sqlite3.connect(f"file:{_resolve_db_path()}?mode=ro", uri=True,
                           timeout=2, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _envelope(started: float, report_date: str, issues: list, data_asof: dict,
              command: str = "issues daily") -> dict:
    by_sev = {}
    for it in issues:
        by_sev[it["severity"]] = by_sev.get(it["severity"], 0) + 1
    return {"schema_version": SCHEMA_VERSION, "ok": True,
            "command": command,
            "elapsed_ms": int((time.time() - started) * 1000),
            "report_date": report_date,
            "data_asof": data_asof,
            "summary": {"total": len(issues), "by_severity": by_sev},
            "issues": issues}

def _error(started: float, code: str, message_zh: str,
           command: str = "issues daily") -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": False,
            "command": command,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": {"code": code, "message_zh": message_zh}}

def _emit(ctx, data: dict, exit_code: int = 0):
    if ctx.obj.get("json"):
        click.echo(json.dumps(data, ensure_ascii=False))
    else:
        human = data.pop("_human", None)
        click.echo(human if human else json.dumps(data, ensure_ascii=False))
    if exit_code:
        raise SystemExit(exit_code)

def _issue(source: str, severity: str, title_zh: str, detail_zh: str,
           dedup_key: str, evidence: dict, suggested_action_zh: str,
           first_seen: str | None = None) -> dict:
    """统一 issue 结构 — dedup_key 是台账幂等与跨天去重的唯一依据。"""
    return {"issue_id": dedup_key, "source": source, "severity": severity,
            "title_zh": title_zh, "detail_zh": detail_zh,
            "evidence": evidence, "suggested_action_zh": suggested_action_zh,
            "dedup_key": dedup_key,
            "first_seen": first_seen or _today(), "occurrences_7d": 1}

def _compact_date(date: str) -> str:
    return date.replace("-", "")

def _collect_error_issue(name: str, date: str, err: Exception) -> dict:
    """单源采集失败降级为 warning issue, 不拖垮全报告 (铁律#12 不可静默)。"""
    return _issue(
        source=name, severity="warning",
        title_zh=f"问题源 {name} 采集失败",
        detail_zh=str(err)[:300],
        dedup_key=f"S0:{name}:collect_error:{_compact_date(date)}",
        evidence={"error": str(err)[:300]},
        suggested_action_zh="检查该表 schema 是否迁移缺失 (DL-43 家族: 迁移脚本写了从未跑)")

# ── 收集器: 每源一个, 签名 collector(conn, date) -> (issues, asof) ──

def collect_pipeline_runs(conn, date: str) -> tuple[list, str | None]:
    """S1 管道失败/部分 — 当日 failed/partial 全收。

    skipped 口径 (锁窗 mass-skip / 依赖链头) 归批 3 降噪闸, 批 1 不收 —
    skipped 多数是依赖守卫正常拦截, 裸收会刷屏 (08-26 锁窗 16 条实证)。
    """
    rows = conn.execute(
        "SELECT pipeline_name, status, error_msg, run_id, created_at "
        "FROM pipeline_runs WHERE date(date) = ? AND status IN ('failed', 'partial') "
        "ORDER BY created_at", (date,)).fetchall()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM pipeline_runs").fetchone()[0]
    issues = []
    for r in rows:
        name, status = r["pipeline_name"], r["status"]
        sev = "critical" if status == "failed" else "warning"
        err = (r["error_msg"] or "")[:300]
        issues.append(_issue(
            source="pipeline_runs", severity=sev,
            title_zh=f"{name} 管道{('失败' if status == 'failed' else '部分成功')}",
            detail_zh=err or "(无错误详情)",
            dedup_key=f"S1:{name}:{status}:{_compact_date(date)}",
            evidence={"run_id": r["run_id"], "error_msg": err,
                      "sql": "SELECT * FROM pipeline_runs WHERE run_id='...'"},
            suggested_action_zh=_suggest_pipeline(err)))
    return issues, asof

def collect_concept_fallback_regression(conn, date: str) -> tuple[list, str | None]:
    """R4 concept 断电案写后校验哨兵 (统筹 sm-39c8b41402a748ee ①, #44 同窗件)。

    语义: 当日 concept_daily 若出现 industry_fallback 行 = 回归信号。
    两种成因都不静默:
      a) 时序颠倒 — 16:35 小管道写真概念后, 15:30 DDP 的 concept child
         (ConceptPipeline 本地 fallback, INSERT OR IGNORE) 晚到把 fallback
         行混回 (次日排程序会自然对齐, 但当日须可观测);
      b) 小管道未跑/失败 — 真概念缺位, 只剩 fallback 冒充 (08-28/31
         断电案原态, coverage 守卫修严后 walk_forward 会硬失败, 本哨兵
         提前一层给运维口径)。

    阈值: 任意 fallback 行 >0 即收 (5892 行/日是断电案全量, 单行也是
    回归信号 — 宁可早叫不可假绿)。asof = concept_daily 当日最新行时间
    不可得, 用管道侧 created_at 近似 (无则 None)。
    """
    try:
        n_fb = conn.execute(
            "SELECT COUNT(*) FROM concept_daily WHERE date = ? "
            "AND source = 'industry_fallback'", (date,)).fetchone()[0]
        n_true = conn.execute(
            "SELECT COUNT(*) FROM concept_daily WHERE date = ? "
            "AND source LIKE 'zzshare%'", (date,)).fetchone()[0]
    except sqlite3.OperationalError:
        return [], None  # 表不存在 = 旧库, 无概念面可校验 (不是回归; 采集
        # 异常已由 _collect_all 外层降级为 warning issue, 铁律 #12 不静默)
    if not n_fb:
        return [], None
    asof = None
    try:
        asof = conn.execute(
            "SELECT MAX(created_at) FROM pipeline_runs "
            "WHERE pipeline_name = 'concept_plates_daily' "
            "AND date = ? ", (date,)).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    sev = "critical" if not n_true else "warning"
    title = ("概念数据断电回归: 当日零真概念" if not n_true
             else "概念数据时序颠倒: fallback 行混回")
    detail = (f"concept_daily {date}: industry_fallback={n_fb} 行, "
              f"zzshare 真概念={n_true} 行 — "
              + ("16:35 concept_plates_daily 未跑或失败 (断电案 08-28/31 原态), "
                 "walk_forward 覆盖守卫将硬失败" if not n_true
                 else "16:35 小管道真概念已写, 但 DDP concept child 的 "
                      "INSERT OR IGNORE fallback 行晚到混回; 次日排程自然对齐, "
                      "当日口径以真源行为准 (守卫只认真源)"))
    return [_issue(
        source="concept_daily_fallback_regression", severity=sev,
        title_zh=title,
        detail_zh=detail,
        dedup_key=f"S12:concept_fallback:{_compact_date(date)}",
        evidence={"sql": "SELECT source, COUNT(*) FROM concept_daily "
                         "WHERE date = ? GROUP BY source",
                  "industry_fallback_rows": n_fb, "zzshare_rows": n_true},
        suggested_action_zh=("查 pipeline_runs 当日 concept_plates_daily 行: "
                             "无行=排程未挂/失败, success 后仍混回=时序颠倒 "
                             "(child 晚到, 观察次日自愈; 连续两日=报统筹裁排程"))], asof


def _suggest_pipeline(err: str) -> str:
    """规则库 — 按 error 模式匹配已知病灶; 无匹配输出通用指引, 不编造根因。"""
    e = err.lower()
    if "closed" in e and "database" in e:
        return ("疑似 closed-database 家族 (aa25e99): get_connection 缓存连接被 "
                "orchestrator close 毒化 — 检查调用链是否复用已关闭连接")
    if "locked" in e:
        return "疑似撞锁窗: 避开 16:00-17:30 重跑; 检查 flock_guard 与依赖守卫配置"
    if "timeout" in e or "timed out" in e:
        return "超时: 检查 ops.yaml 该管道 timeout_s 与上游依赖 wait_timeout_s 链"
    return "查 logs/pipeline.log 该 run_id 上下文; 连续出现则升级统筹派单"

def collect_alerts(conn, date: str) -> tuple[list, str | None]:
    """S2 未闭环告警 — 降噪闸②: 僵尸告警折叠。

    僵尸 = open 但健康证据 healthy=True (已修复未 resolve, 08-21 intraday_scan
    旧账 20 条刷屏实证) → 折叠成一条 info 汇总不刷屏, resolve 裁定归统筹;
    其余 (仍异常/证据不足) 逐条报。P0-3 三态去重口径随批 4 台账接入。
    """
    rows = conn.execute(
        "SELECT id, severity, layer, title, detail, created_at "
        "FROM alerts WHERE status = 'open' ORDER BY severity, created_at").fetchall()
    asof = conn.execute("SELECT MAX(created_at) FROM alerts").fetchone()[0]
    issues = []
    zombies = []
    for r in rows:
        ev = _alert_health_evidence(conn, r["title"], r["created_at"])
        if ev.get("healthy") is True:
            zombies.append(r)
            continue
        sev = "critical" if r["severity"] == "critical" else "warning"
        title = r["title"] or "(无标题)"
        issues.append(_issue(
            source="alerts", severity=sev,
            title_zh=f"[{r['layer'] or '?'}] {title}",
            detail_zh=(r["detail"] or "")[:300] or "(无详情)",
            dedup_key=f"S2:alert{r['id']}:open:{_compact_date(date)}",
            evidence={"alert_id": r["id"], "layer": r["layer"],
                      "created_at": r["created_at"]},
            suggested_action_zh="未闭环告警: 确认是否已处理 — 已处理则 resolve, 未处理转统筹派单"))
    if zombies:
        ids = [r["id"] for r in zombies]
        id_txt = ", ".join(f"#{i}" for i in ids[:20]) + ("…" if len(ids) > 20 else "")
        issues.append(_issue(
            source="alerts", severity="info",
            title_zh=f"僵尸告警 ×{len(zombies)} (已修复未 resolve, 已折叠)",
            detail_zh=f"健康证据显示已恢复, 待统筹裁定统一 resolve: {id_txt}",
            dedup_key=f"S2:zombie_batch:{_compact_date(date)}",
            evidence={"alert_ids": ids, "count": len(zombies)},
            suggested_action_zh="跑 issues stale-alerts 看证据链; 统筹裁定后统一 resolve (勿自行写库)"))
    return issues, asof

def collect_data_quality_flags(conn, date: str) -> tuple[list, str | None]:
    """S3 数据质量旗标 — 降噪闸①: top-5 + 新增 rule 标记 + 余者折叠。

    原始量=噪音 (08-28 实测近 7 天 9544 条): 按 table+rule 聚合后只报 top-5,
    其余折叠成一条 info; 昨日没有今日有的 rule 标 [新增] (新病灶信号优先)。
    """
    rows = conn.execute(
        "SELECT table_name, rule, COUNT(*) AS n FROM data_quality_flags "
        "WHERE date(created_at) = ? GROUP BY table_name, rule "
        "ORDER BY n DESC", (date,)).fetchall()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM data_quality_flags").fetchone()[0]
    if not rows:
        return [], asof
    # 昨日 rule 集合 — 用于新增 rule 识别
    yday = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    yday_rules = {r[0] for r in conn.execute(
        "SELECT DISTINCT rule FROM data_quality_flags "
        "WHERE date(created_at) = ?", (yday,))}
    top, rest = rows[:5], rows[5:]
    issues = []
    for r in top:
        n = r["n"]
        is_new = r["rule"] not in yday_rules
        sev = "critical" if n >= 100 else "warning"
        tag = " [新增]" if is_new else ""
        issues.append(_issue(
            source="data_quality_flags", severity=sev,
            title_zh=f"{r['table_name']} 质量旗标 {r['rule']} ×{n}{tag}",
            detail_zh=f"当日 {r['table_name']}.{r['rule']} 触发 {n} 次"
                      + ("; 昨日无此规则 — 新病灶信号优先排查" if is_new else ""),
            dedup_key=f"S3:{r['table_name']}:{r['rule']}:{_compact_date(date)}",
            evidence={"table_name": r["table_name"], "rule": r["rule"],
                      "count": n, "is_new_rule": is_new},
            suggested_action_zh=f"抽查 {r['table_name']} 该规则命中样本; 大量级(≥100)优先查数据源异常"))
    if rest:
        rest_n = sum(r["n"] for r in rest)
        issues.append(_issue(
            source="data_quality_flags", severity="info",
            title_zh=f"质量旗标余 {len(rest)} 组 ×{rest_n} (已折叠)",
            detail_zh="top-5 之外的低频组: " + ", ".join(
                f"{r['table_name']}.{r['rule']}×{r['n']}" for r in rest[:8])
                + ("…" if len(rest) > 8 else ""),
            dedup_key=f"S3:rest_folded:{_compact_date(date)}",
            evidence={"groups": [{"table_name": r["table_name"], "rule": r["rule"],
                                  "count": r["n"]} for r in rest],
                      "total_count": rest_n},
            suggested_action_zh="低频组抽查即可; 某组连续多日上升再升级"))
    return issues, asof

def collect_freshness_audit(conn, date: str) -> tuple[list, str | None]:
    """S4 新鲜度审计 — 当日 delayed/missing。"""
    rows = conn.execute(
        "SELECT table_name, status, delay_min, expected_by, actual_at "
        "FROM freshness_audit WHERE date(date) = ? AND status IN ('delayed', 'missing') "
        "ORDER BY status, table_name", (date,)).fetchall()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM freshness_audit").fetchone()[0]
    issues = []
    for r in rows:
        delay = "" if r["delay_min"] is None else f", 延迟 {r['delay_min']} 分钟"
        issues.append(_issue(
            source="freshness_audit", severity="warning",
            title_zh=f"{r['table_name']} 新鲜度{('延迟' if r['status'] == 'delayed' else '缺失')}",
            detail_zh=f"期望 {r['expected_by'] or '?'} 前就绪, 实际 {r['actual_at'] or '无记录'}{delay}",
            dedup_key=f"S4:{r['table_name']}:{r['status']}:{_compact_date(date)}",
            evidence={"table_name": r["table_name"], "status": r["status"],
                      "delay_min": r["delay_min"]},
            suggested_action_zh="查上游管道当日是否跑完; missing 多为上游未产表或看门狗预算<依赖窗 (freshness 盲区家族)"))
    return issues, asof

def collect_quality_checks(conn, date: str) -> tuple[list, str | None]:
    """S5 质量检查红 — 当日 result != 'pass'。"""
    rows = conn.execute(
        "SELECT table_name, check_name, level, result, detail "
        "FROM quality_checks WHERE date(date) = ? AND result != 'pass' "
        "ORDER BY level DESC", (date,)).fetchall()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM quality_checks").fetchone()[0]
    issues = []
    for r in rows:
        sev = "critical" if (r["level"] or "").lower() in ("error", "critical") else "warning"
        issues.append(_issue(
            source="quality_checks", severity=sev,
            title_zh=f"{r['table_name']} 质检 {r['check_name']} = {r['result']}",
            detail_zh=(r["detail"] or "")[:300] or "(无详情)",
            dedup_key=f"S5:{r['table_name']}:{r['check_name']}:{_compact_date(date)}",
            evidence={"table_name": r["table_name"], "check_name": r["check_name"],
                      "result": r["result"]},
            suggested_action_zh="质检红: 按 check_name 定位规则, 确认是数据真坏还是规则误报"))
    return issues, asof

def collect_health_history(conn, date: str) -> tuple[list, str | None]:
    """S6 健康检查 — 最近一次 overall 非 green, 展开 layer_statuses。"""
    row = conn.execute(
        "SELECT check_time, overall_status, layer_statuses FROM health_history "
        "ORDER BY check_time DESC LIMIT 1").fetchone()
    if row is None:
        return [], None
    bad = {}
    try:
        layers = json.loads(row["layer_statuses"] or "{}")
        bad = {k: v for k, v in layers.items() if v not in ("green", None)}
    except (json.JSONDecodeError, TypeError):
        pass
    if row["overall_status"] == "green" and not bad:
        return [], row["check_time"]
    sev = "critical" if (row["overall_status"] == "red" or "red" in bad.values()) else "warning"
    bad_txt = ", ".join(f"{k}={v}" for k, v in bad.items()) or "(层级明细缺失)"
    return [_issue(
        source="health_history", severity=sev,
        title_zh=f"健康检查 {row['overall_status']}",
        detail_zh=f"最近一次 {row['check_time']}: {bad_txt}",
        dedup_key=f"S6:health:{row['overall_status']}:{_compact_date(date)}",
        evidence={"check_time": row["check_time"],
                  "overall_status": row["overall_status"], "bad_layers": bad},
        suggested_action_zh="按红/黄层查对应层当日管道与告警; data 层红常与 freshness/S3 同源")], row["check_time"]

def collect_delivery_log(conn, date: str) -> tuple[list, str | None]:
    """S7 投递失败 — 当日 failed (飞书推送断链=用户直接失明, severity 提一级=warning 起)。"""
    rows = conn.execute(
        "SELECT id, content_type, channel, error, created_at "
        "FROM delivery_log WHERE date(created_at) = ? AND status = 'failed' "
        "ORDER BY created_at", (date,)).fetchall()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM delivery_log").fetchone()[0]
    issues = []
    for r in rows:
        issues.append(_issue(
            source="delivery_log", severity="warning",
            title_zh=f"投递失败: {r['content_type']} → {r['channel']}",
            detail_zh=(r["error"] or "")[:300] or "(无错误详情)",
            dedup_key=f"S7:delivery{r['id']}:failed:{_compact_date(date)}",
            evidence={"delivery_id": r["id"], "content_type": r["content_type"],
                      "channel": r["channel"]},
            suggested_action_zh="推送断链=用户失明: 查飞书 token/网络; 连续失败升级 critical"))
    return issues, asof

def collect_e2e_verification(conn, date: str) -> tuple[list, str | None]:
    """S8 e2e 链路验证 — logs/e2e_verification.jsonl 最近一条当日记录, 非全绿即报。"""
    path = os.path.join(_resolve_logs_dir(), "e2e_verification.jsonl")
    if not os.path.exists(path):
        return [], None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("date") == date:
                last = rec  # 逐行覆盖 → 留当日最后一条
    if last is None:
        return [], None
    fails, warns = last.get("fails", 0), last.get("warns", 0)
    if fails == 0 and warns == 0:
        return [], last.get("timestamp")
    bad_checks = [c for c in last.get("checks", [])
                  if c.get("status") in ("fail", "warn")][:10]
    sev = "critical" if fails > 0 else "warning"
    detail = "; ".join(f"{c.get('name')}={c.get('status')}: {(c.get('detail') or '')[:80]}"
                       for c in bad_checks) or "(无明细)"
    return [_issue(
        source="e2e_verification", severity=sev,
        title_zh=f"e2e 链路验证 {last.get('phase')} 相: {fails} 失败 {warns} 警告",
        detail_zh=detail[:300],
        dedup_key=f"S8:e2e:{last.get('phase')}:{_compact_date(date)}",
        evidence={"phase": last.get("phase"), "fails": fails, "warns": warns,
                  "bad_checks": bad_checks},
        suggested_action_zh="按失败项查对应管道 pipeline_runs; warns 多为日志缺记录非真失败")], last.get("timestamp")

MASS_SKIP_THRESHOLD = 5  # 同一 10 分钟窗内 ≥5 个不同键被跳 = mass-skip 事故模式

def collect_watchdog(conn, date: str) -> tuple[list, str | None]:
    """S9 watchdog/cron 失败 — logs/pipeline.log 当日 skipped_lock / KILLED 行。

    降噪闸③: mass-skip 聚合 — 同一 10 分钟窗内 ≥5 个不同键被跳 (08-26 锁窗
    吞没 16 作业实证) 折叠成一条 critical; 其余 skip 按 key 聚合 warning;
    KILLED (budget 超时被杀) 逐条 critical。
    """
    path = os.path.join(_resolve_logs_dir(), "pipeline.log")
    if not os.path.exists(path):
        return [], None
    # skip 行: (10分钟窗, key) 计数; killed 行原样收
    windows: dict[str, dict[str, int]] = {}
    killed: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith(date):
                continue
            if "skipped_lock" in line:
                m = re.search(r"key=(\S+)", line)
                key = m.group(1) if m else "?"
                tm = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", line)
                win = f"{tm.group(1)}:{int(tm.group(2)) // 10 * 10:02d}" if tm else "?"
                windows.setdefault(win, {})
                windows[win][key] = windows[win].get(key, 0) + 1
            elif "KILLED" in line:
                killed.append(line.strip()[:200])
    issues = []
    mass_keys: set[str] = set()
    for win, keys in sorted(windows.items()):
        if len(keys) >= MASS_SKIP_THRESHOLD:
            mass_keys |= set(keys)
            total = sum(keys.values())
            issues.append(_issue(
                source="watchdog", severity="critical",
                title_zh=f"mass-skip: {win} 窗口 {len(keys)} 键被锁窗吞没 ×{total}",
                detail_zh="键: " + ", ".join(sorted(keys)[:12])
                          + ("…" if len(keys) > 12 else ""),
                dedup_key=f"S9:mass_skip:{win}:{_compact_date(date)}",
                evidence={"window": win, "keys": sorted(keys), "total": total},
                suggested_action_zh=("08-26 事故模式: 锁主持有时间吞没下游作业 — "
                                     "查锁主管道耗时与预算公式, 评估补跑链")))
    for win, keys in sorted(windows.items()):
        for key, n in sorted(keys.items(), key=lambda kv: -kv[1]):
            if key in mass_keys:
                continue  # 已并入 mass-skip 一条, 不重复刷屏
            issues.append(_issue(
                source="watchdog", severity="warning",
                title_zh=f"锁窗跳过: {key} ×{n}",
                detail_zh=f"flock_guard 当日跳过 {key} {n} 次 (锁主仍在运行)",
                dedup_key=f"S9:skipped_lock:{key}:{_compact_date(date)}",
                evidence={"key": key, "count": n},
                suggested_action_zh="单键 skip 多为正常锁等待; 连续多日或升级 mass-skip 再查"))
    for i, line in enumerate(killed[:5]):
        m = re.search(r"key=(\S+)", line)
        key = m.group(1) if m else "?"
        issues.append(_issue(
            source="watchdog", severity="critical",
            title_zh=f"watchdog 强杀: {key}",
            detail_zh=line,
            dedup_key=f"S9:killed:{key}:{i}:{_compact_date(date)}",
            evidence={"log_line": line},
            suggested_action_zh="budget 超时被杀: 查该管道耗时膨胀 (OOM 模式=日志块缓冲不涨≠进程死); 考虑分片"))
    return issues, None

def _purgedcv_cfg() -> dict:
    """F1 哨兵配置读取 — ops.yaml 顶层段 purged_cv_upgrade (设计稿 §2.1)。

    自持 yaml 分层读 (defaults/*.yaml 排序 → settings → profiles/{profile}
    → local), 合并序与 config_layer/loader.py:load_profile 完全一致,
    全键空域语义等价 — 但不走 Config 对象: issues 是只读直驱命令组
    (不 bootstrap), CONFIG_ENV=prod 下 Config.load() 会触发 CF-01 七键
    硬失败 RuntimeError, 而本文件 _collect_all 外层只兜
    sqlite3.OperationalError — cfg_get 一炸全报告炸 (实测证伪 09-03)。
    故镜像 advisory「独立拷贝自治」宪法: 本函数独立读 yaml, 顶层段
    全键空域, 禁 pipeline-block-scope (D-293 证伪陷阱面)。
    缺段/缺键全默认: state=lightweight, threshold=50, window_end=None。
    """
    cfg = {"state": "lightweight", "oos_trade_threshold": 50,
           "stable_window_end": None}
    try:
        import yaml
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        config_dir = os.environ.get(
            "CONFIG_DIR", os.path.join(repo, "config"))
        profile = os.environ.get("CONFIG_ENV", "dev")

        def _merge(base: dict, overlay: dict) -> dict:
            for k, v in (overlay or {}).items():
                if isinstance(base.get(k), dict) and isinstance(v, dict):
                    _merge(base[k], v)
                else:
                    base[k] = v
            return base

        merged: dict = {}
        defaults_dir = os.path.join(config_dir, "defaults")
        if os.path.isdir(defaults_dir):
            for fn in sorted(os.listdir(defaults_dir)):
                if fn.endswith(".yaml"):
                    try:
                        with open(os.path.join(defaults_dir, fn),
                                  encoding="utf-8") as f:
                            _merge(merged, yaml.safe_load(f) or {})
                    except OSError:
                        pass
        for rel in ("settings.yaml",
                    os.path.join("profiles", f"{profile}.yaml"),
                    "local.yaml"):
            path = os.path.join(config_dir, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    _merge(merged, yaml.safe_load(f) or {})
            except OSError:
                pass
        seg = merged.get("purged_cv_upgrade") or {}
        if isinstance(seg, dict):
            if "state" in seg:
                cfg["state"] = str(seg["state"])
            if seg.get("oos_trade_threshold") is not None:
                cfg["oos_trade_threshold"] = int(seg["oos_trade_threshold"])
            if seg.get("stable_window_end") is not None:
                cfg["stable_window_end"] = str(seg["stable_window_end"])
    except Exception:
        pass  # 任何配置面异常 → 三键默认 (哨兵活性优先, 不炸宿主报告)
    return cfg

def collect_purgedcv_upgrade_sentinel(
        conn, date: str,
        state: str | None = None,
        oos_trade_threshold: int | None = None,
        stable_window_end: str | None = None) -> tuple[list, str | None]:
    """F1 purgedcv 轻量版升级哨兵 (D-291 期1配套, 统筹 sm-58814c13363f4078)。

    语义 (设计稿 §2.2): 用户自述业余开发有忘失风险, 升级不能依赖人记 —
    本哨兵=防忘形态本体, render-time 无状态重算, 每晚 21:07 issues
    daily 重查: 条件仍满足就重报, 直到 ops.yaml state 键翻转。

    前置熄灭: state != lightweight → 空 (strict=已升级; keep_light=用户
    明裁保留轻量, 哨兵静默留档)。缺 state 键 = lightweight 默认 (同义)。

    T1 (交易量触发): walk_forward OOS 累计 total_trades >= 阈值 (50)。
    口径钉死 (§1.2): SELECT COALESCE(SUM(total_trades),0) FROM
    backtest_runs WHERE mode='walk_forward' — daily_backtest 日频考试
    与 hold 持有期检验不属折叠口径, 不入计数 (宁可少算不虚涨)。

    T2 (稳定窗后首考触发): stable_window_end 非 null 且 当日 >= 窗末日
    且 EXISTS walk_forward run created_at >= 窗末日 (窗后考试已发生)。
    两者先到先触发; severity=warning; dedup_key 固定跨夜同键。

    参数注入: state/threshold/window_end 显式传参时覆盖配置面 (测试
    夹具注入口); 生产路径 None → 读 ops.yaml 顶层段 (经 _purgedcv_cfg)。
    旧库无 backtest_runs 表 → 无 issue 不炸 (不是回归, 外层已降级)。
    """
    cfg = _purgedcv_cfg()
    state = state if state is not None else cfg["state"]
    if oos_trade_threshold is None:
        oos_trade_threshold = cfg["oos_trade_threshold"]
    if stable_window_end is None:
        stable_window_end = cfg["stable_window_end"]

    if state != "lightweight":
        return [], None  # 前置熄灭: strict/keep_light 均静默

    oos_trades = 0
    trigger = None
    try:
        oos_trades = conn.execute(
            "SELECT COALESCE(SUM(total_trades),0) FROM backtest_runs "
            "WHERE mode = 'walk_forward'").fetchone()[0]
        if (oos_trades or 0) >= (oos_trade_threshold or 50):
            trigger = "t1_trades"
        if trigger is None and stable_window_end:
            if (date or "") >= stable_window_end:
                first_exam = conn.execute(
                    "SELECT 1 FROM backtest_runs WHERE mode = 'walk_forward' "
                    "AND created_at >= ? LIMIT 1",
                    (stable_window_end,)).fetchone()
                if first_exam:
                    trigger = "t2_window"
    except sqlite3.OperationalError:
        return [], None  # 无表/旧库: 不是回归, 不炸宿主
    if trigger is None:
        return [], None

    if trigger == "t1_trades":
        title_zh = (f"F1 purgedcv 升级哨兵: OOS 累计 {oos_trades}/"
                    f"{oos_trade_threshold} 笔达阈, 轻量版该升严格版")
        detail_zh = (f"walk_forward 折叠口径 OOS 累计交易 {oos_trades} 笔已达 "
                     f"阈值 {oos_trade_threshold} (D-291 拍板: 50 笔=轻量版已"
                     "见过足够样本)。两触发器口径: T1 交易量达阈 / T2 稳定窗"
                     "(stable_window_end) 后首考已发生, 先到先触发。")
    else:
        title_zh = "F1 purgedcv 升级哨兵: 稳定窗后首考已发生, 轻量版该升严格版"
        detail_zh = (f"稳定窗窗末日 {stable_window_end} 已到且窗后已有 "
                     "walk_forward 首考 (T2 触发器, D-291 双判据之一)。"
                     f"当前 OOS 累计 {oos_trades} 笔 (阈值 "
                     f"{oos_trade_threshold}, T1 未达则由 T2 触发)。"
                     "改基勘正 (sm-5ab96e9e15aa4123): 锚=回补批完成日 08-26 "
                     "(jsonl run_finished 08-26 00:31:17 物证), 窗末=锚+14=09-09; "
                     "旧锚 08-29 系 GO-9 卷宗窗口包日期误标为批完成日 (GO-9 已销案, "
                     "回补批已吃尽可填史 — 实填 7,870,684 cells, 残NULL≈15,551,557 "
                     "为估算口径非填充量), 勘正记档。")
    evidence = {"oos_trades": oos_trades,
                "oos_trade_threshold": oos_trade_threshold,
                "trigger": trigger, "state": state,
                "sql": "SELECT COALESCE(SUM(total_trades),0) FROM "
                       "backtest_runs WHERE mode='walk_forward'"}
    if stable_window_end:
        evidence["stable_window_end"] = stable_window_end
    return [_issue(
        source="purgedcv_upgrade_sentinel", severity="warning",
        title_zh=title_zh, detail_zh=detail_zh,
        dedup_key="purgedcv-upgrade-sentinel",   # 固定键: 跨夜同键, 台账幂等
        evidence=evidence,
        suggested_action_zh="升级卡 (隔离带版 purgedcv: 概念进出登记簿+隔离带) "
                            "验收面必须含 ops.yaml state: lightweight→strict "
                            "翻转, 否则本哨兵每晚重报 (防忘特性非 bug); "
                            "用户明裁保留轻量则落 keep_light 静默留档")], None

def collect_gate(conn, date: str) -> tuple[list, str | None]:
    """S10 门禁红 — logs/gate_summary.json (统筹门禁跑完写; 不存在=未跑, 静默跳过)。"""
    path = os.path.join(_resolve_logs_dir(), "gate_summary.json")
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, encoding="utf-8") as f:
            gate = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return [_collect_error_issue("gate", date, e)], None
    fails, errors = gate.get("failed", 0), gate.get("errors", 0)
    if fails == 0 and errors == 0:
        return [], gate.get("finished_at")
    return [_issue(
        source="gate", severity="warning",
        title_zh=f"门禁非全绿: {fails}F {errors}E / {gate.get('passed', '?')}P",
        detail_zh=f"基线 0F/0E; 失败: {', '.join((gate.get('failures') or [])[:5]) or '(无明细)'}",
        dedup_key=f"S10:gate:{_compact_date(gate.get('date') or date)}",
        evidence={"passed": gate.get("passed"), "failed": fails,
                  "errors": errors, "commit": gate.get("commit")},
        suggested_action_zh="对照基线 0F/0E/3578P; 存量环境性红须基线复跑定性 (08-26 先例), 勿直接修")], gate.get("finished_at")

def collect_human_review_queue(conn, date: str) -> tuple[list, str | None]:
    """S11 审批队列积压 — pending 滞留 >24h (自动审批链断流信号)。"""
    rows = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM human_review_queue "
        "WHERE status = 'pending' AND created_at < datetime('now', '-24 hours')").fetchone()
    asof = conn.execute(
        "SELECT MAX(created_at) FROM human_review_queue").fetchone()[0]
    n = rows["n"] if rows else 0
    if n == 0:
        return [], asof
    return [_issue(
        source="human_review_queue", severity="warning",
        title_zh=f"审批队列积压: {n} 条 pending 超 24h",
        detail_zh=f"最早积压 {rows['oldest']}; 自动审批链 (Phase1.6) 应趋零, 积压=断流",
        dedup_key=f"S11:hrq_backlog:{_compact_date(date)}",
        evidence={"count": n, "oldest": rows["oldest"]},
        suggested_action_zh="查 hitl daemon 是否在跑 + 自动审批开关; 积压件按清算方案处理 (19作废+9自动批先例)")], asof

def collect_chart_shadow(conn, date: str) -> tuple[list, str | None]:
    """#101 切3 影子模式观察项 (LLM 读图, 设计稿 §5/§6)。

    两档语义:
      info    — 当日 chart_analysis 有行 → 双录覆盖一条 info (影子期日常
                 口径, 6 周裁决窗内每天可见, 裁决日取数直接 SELECT);
      warning — 当日候选非零 (signal_provenance ∪ stock_pool, 切2
                 load_candidates 同口径) 但 chart_analysis 零行 = 读图
                 管道未跑/失败 (表建未通电复燃)。
    旧库无表 → 无 issue 不炸 (S12 同语义: 不是回归); degraded 只入证据面
    不判级 (6 周窗内 LLM 通道抖动属预期, 不收 issue 刷屏, 裁决日聚合)。
    """
    try:
        n_total = conn.execute(
            "SELECT COUNT(*) FROM chart_analysis WHERE date = ?",
            (date,)).fetchone()[0]
    except sqlite3.OperationalError:
        return [], None   # 表未迁移 = 旧库, 无读图面可观测 (不是回归)
    if n_total:
        n_base = conn.execute(
            "SELECT COUNT(*) FROM chart_analysis "
            "WHERE date = ? AND source = 'atr_floor'", (date,)).fetchone()[0]
        n_llm = conn.execute(
            "SELECT COUNT(*) FROM chart_analysis "
            "WHERE date = ? AND source = 'llm_chart' AND pattern IS NOT NULL",
            (date,)).fetchone()[0]
        n_degraded = conn.execute(
            "SELECT COUNT(*) FROM chart_analysis "
            "WHERE date = ? AND source = 'llm_chart' AND pattern IS NULL",
            (date,)).fetchone()[0]
        asof = conn.execute(
            "SELECT MAX(created_at) FROM chart_analysis WHERE date = ?",
            (date,)).fetchone()[0]
        return [_issue(
            source="chart_analysis_shadow", severity="info",
            title_zh=f"影子模式双录观测: chart_analysis {date} {n_total} 行",
            detail_zh=(f"atr_floor={n_base} / llm_chart={n_llm} / "
                       f"degraded={n_degraded} — 6 周裁决窗三率对照见 "
                       "p2_factors 影子段 (裁决前 LLM 结论不入账)"),
            dedup_key=f"S13:chart_shadow:{_compact_date(date)}",
            evidence={"atr_floor_rows": n_base, "llm_chart_rows": n_llm,
                      "degraded_rows": n_degraded},
            suggested_action_zh=("影子期日常口径, 无需处置; degraded 持续偏高 "
                                 "再查 LLM 通道 (裁决日按证据聚合)"))], asof
    # 零行 → 查候选 (切2 load_candidates 同口径: 当日信号 ∪ 未出场池)
    try:
        n_cand = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT code FROM signal_provenance "
            "WHERE date = ? UNION SELECT code FROM stock_pool "
            "WHERE status NOT IN ('exited'))", (date,)).fetchone()[0]
    except sqlite3.OperationalError:
        return [], None   # 候选源缺 = 旧库形态, 不判级
    if not n_cand:
        return [], None   # 假日/未排程日: 无信号无读图, 不发空警
    return [_issue(
        source="chart_analysis_shadow", severity="warning",
        title_zh=f"读图断电: {date} 候选 {n_cand} 只但 chart_analysis 零行",
        detail_zh="信号候选非零但当日无读图记录 — 读图管道未跑或失败 (表建未通电形态)",
        dedup_key=f"S13:chart_shadow:{_compact_date(date)}",
        evidence={"candidate_count": n_cand},
        suggested_action_zh=("查 orchestrator 当日 chart 段 dispatch/执行回执; "
                             "连续两日=报统筹裁排程"))], None

# 收集器注册表 — 顺序即报告顺序 (批 2: 11 源全接入)
COLLECTORS = [
    ("pipeline_runs", collect_pipeline_runs),
    # R4 concept 断电案 (统筹 sm-39c8b41402a748ee ①): 17:05 写后校验哨兵 —
    # 当日 industry_fallback 行再现 = 16:35 小管道写完真概念后被 DDP concept
    # child 的 INSERT OR IGNORE 混回 (时序颠倒), 或小管道未跑 — 报 issues 非静默。
    ("concept_daily_fallback_regression", collect_concept_fallback_regression),
    ("alerts", collect_alerts),
    ("data_quality_flags", collect_data_quality_flags),
    ("freshness_audit", collect_freshness_audit),
    ("quality_checks", collect_quality_checks),
    ("health_history", collect_health_history),
    ("delivery_log", collect_delivery_log),
    ("e2e_verification", collect_e2e_verification),
    ("watchdog", collect_watchdog),
    ("gate", collect_gate),
    ("human_review_queue", collect_human_review_queue),
    # F1 purgedcv 升级哨兵 (D-291 期1配套, 统筹 sm-58814c13363f4078):
    # 注册即每晚 21:07 自动出现在日报 = 防忘形态本体; 其余 12 源零改。
    ("purgedcv_upgrade_sentinel", collect_purgedcv_upgrade_sentinel),
    # #101 切3 影子模式观察项 — chart_analysis 通电观测 (info) + 断电回归 (warning)
    ("chart_analysis_shadow", collect_chart_shadow),
]

# ── 人类可读渲染 (全中文) ──

def _render_human(env: dict) -> str:
    lines = [f"📋 QuantStar 每日问题报告 — {env['report_date']}",
             f"共 {env['summary']['total']} 个问题 "
             f"(critical {env['summary']['by_severity'].get('critical', 0)} / "
             f"warning {env['summary']['by_severity'].get('warning', 0)})"]
    if not env["issues"]:
        lines.append("✅ 今日无问题")
    for i, it in enumerate(env["issues"], 1):
        icon = "🔴" if it["severity"] == "critical" else "🟡"
        lines.append(f"\n{i}. {icon} {it['title_zh']}")
        lines.append(f"   详情: {it['detail_zh']}")
        lines.append(f"   建议: {it['suggested_action_zh']}")
    return "\n".join(lines)

@click.group()
def issues():
    """每日问题报告 (只读) — 聚合系统问题源, 供用户与 quantmaster 调用"""

def _collect_all(date: str) -> tuple[list, dict]:
    """聚合全部问题源 — 返回 (issues, data_asof); DB 连接失败抛 OperationalError。"""
    conn = _ro_connect()
    all_issues, data_asof = [], {}
    try:
        for name, collector in COLLECTORS:
            try:
                iss, asof = collector(conn, date)
                all_issues.extend(iss)
                if asof:
                    data_asof[name] = asof
            except sqlite3.OperationalError as e:
                all_issues.append(_collect_error_issue(name, date, e))
    finally:
        conn.close()
    return all_issues, data_asof

@issues.command()
@click.option("--date", "date", default=None, help="报告日期 YYYY-MM-DD (默认今天)")
@click.pass_context
def daily(ctx, date):
    """聚合当日全部问题源 → 中文报告 / --json 稳定 schema"""
    started = time.time()
    if date is None:
        date = _today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        _emit(ctx, _error(started, "BAD_DATE", f"日期格式错误: {date} (应为 YYYY-MM-DD)"), 2)
        return
    try:
        all_issues, data_asof = _collect_all(date)
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    env = _envelope(started, date, all_issues, data_asof)
    env["_human"] = _render_human(dict(env))
    _emit(ctx, env, 0)

@issues.command()
@click.option("--date", "date", default=None, help="报告日期 YYYY-MM-DD (默认今天)")
@click.pass_context
def sync(ctx, date):
    """聚合 + 幂等写入 Bitable 问题台账 (唯一写动作; 需台账配置 + app 可编辑权限)

    D1 裁定: quantmaster 每晚代跑 (与 daily 同入口, 失败一起暴露)。
    info 级折叠汇总不入台账; dedup_key 已存在只更新 last_seen/occurrences,
    不重置 status (统筹已派单的件不被打回 pending)。
    """
    started = time.time()
    if date is None:
        date = _today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        _emit(ctx, _error(started, "BAD_DATE", f"日期格式错误: {date} (应为 YYYY-MM-DD)",
                          command="issues sync"), 2)
        return
    try:
        all_issues, data_asof = _collect_all(date)
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(started, "DB_ERROR", f"数据库连接失败: {e}",
                          command="issues sync"), 1)
        return
    from cli.issues_ledger import sync_issues
    result = sync_issues(all_issues)
    env = _envelope(started, date, all_issues, data_asof, command="issues sync")
    env["sync_result"] = result
    env["_human"] = (
        _render_human(dict(env)) + "\n\n"
        + (f"📤 台账同步: 新增 {result.get('created', 0)} / "
           f"更新 {result.get('updated', 0)} / info 跳过 {result.get('skipped_info', 0)}"
           + (f" / 失败 {len(result.get('errors', []))}" if result.get("errors") else ""))
        if result.get("ok") else
        f"📤 台账同步失败: {result.get('message_zh', result.get('code'))}")
    # 台账写失败 ≠ 命令坏 (聚合本身成功) — exit 0 但 ok 标记在 sync_result 里,
    # quantmaster 据此向用户说明"报告生成了但台账没写进去"
    _emit(ctx, env, 0)

@issues.command()
@click.pass_context
def pending(ctx):
    """读台账待派单行 (pending/user_escalated/auto_assigned) — 统筹巡检用"""
    started = time.time()
    from cli.issues_ledger import read_pending
    result = read_pending()
    if not result.get("ok"):
        _emit(ctx, _error(started, result.get("code", "LEDGER_ERROR"),
                          result.get("message_zh", "台账读取失败"),
                          command="issues pending"), 1)
        return
    env = {"schema_version": SCHEMA_VERSION, "ok": True,
           "command": "issues pending",
           "elapsed_ms": int((time.time() - started) * 1000),
           "summary": {"total": result["total"]},
           "pending": result["pending"]}
    human = [f"📥 台账待派单 — 共 {result['total']} 条"]
    for r in result["pending"]:
        icon = "🔴" if r["severity"] == "critical" else "🟡"
        human.append(f"{icon} [{r['status']}] {r['title_zh']} "
                     f"(source={r['source']}, 首现={r['first_seen']}, "
                     f"7d×{r['occurrences_7d']}) dedup={r['dedup_key']}")
    if not result["pending"]:
        human.append("✅ 无待派单问题")
    env["_human"] = "\n".join(human)
    _emit(ctx, env, 0)

# ── stale-alerts: 已修复未 resolve 旧账清单 (批 2 附加件, 只读不写库) ──

def _alert_health_evidence(conn, title: str, created_at: str) -> dict:
    """按告警标题模式取健康证据 — 供统筹裁定 resolve, 本命令不写库。"""
    ev: dict = {"pattern": "unknown", "healthy": None}
    since = (created_at or "")[:10]
    m = re.search(r"直连脚本异常: (\S+)", title or "")
    if m:
        script = m.group(1)
        row = conn.execute(
            "SELECT SUM(status='success') AS ok, SUM(status='failed') AS bad, "
            "MAX(created_at) AS last FROM pipeline_runs "
            "WHERE pipeline_name = ? AND date(date) > ?", (script, since)).fetchone()
        ev = {"pattern": "script", "script": script,
              "success_after": row["ok"] or 0, "failed_after": row["bad"] or 0,
              "last_run": row["last"],
              "healthy": (row["ok"] or 0) > 0 and (row["bad"] or 0) == 0}
        return ev
    if "health_red" in (title or ""):
        row = conn.execute(
            "SELECT overall_status, check_time FROM health_history "
            "ORDER BY check_time DESC LIMIT 1").fetchone()
        ev = {"pattern": "health_red",
              "latest_overall": row["overall_status"] if row else None,
              "latest_check": row["check_time"] if row else None,
              "healthy": bool(row and row["overall_status"] == "green")}
        return ev
    if "consecutive_failures" in (title or ""):
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pipeline_runs "
            "WHERE status='failed' AND date(date) > ?", (since,)).fetchone()
        ev = {"pattern": "consecutive_failures",
              "failed_since_alert": row["n"],
              "healthy": row["n"] == 0}
        return ev
    m = re.search(r"管道失败: (\S+)", title or "")
    if m:
        pipe = m.group(1)
        row = conn.execute(
            "SELECT SUM(status='success') AS ok, SUM(status='failed') AS bad, "
            "MAX(created_at) AS last FROM pipeline_runs "
            "WHERE pipeline_name = ? AND date(date) > ?", (pipe, since)).fetchone()
        ev = {"pattern": "pipeline", "pipeline": pipe,
              "success_after": row["ok"] or 0, "failed_after": row["bad"] or 0,
              "last_run": row["last"],
              "healthy": (row["ok"] or 0) > 0 and (row["bad"] or 0) == 0}
        return ev
    if "Ghost" in (title or ""):
        m2 = re.search(r"Ghost 线程: (\S+)", title or "")
        pipe = m2.group(1) if m2 else None
        row = conn.execute(
            "SELECT status, created_at FROM pipeline_runs WHERE pipeline_name = ? "
            "AND created_at > ? ORDER BY created_at DESC LIMIT 1",
            (pipe, created_at)).fetchone() if pipe else None
        ev = {"pattern": "ghost", "pipeline": pipe,
              "last_run_status": row["status"] if row else None,
              "last_run": row["created_at"] if row else None,
              "healthy": bool(row and row["status"] == "success")}
        return ev
    return ev

@issues.command("stale-alerts")
@click.option("--min-age-days", default=1, show_default=True,
              help="告警年龄下限 (天) — 小于此不视为旧账")
@click.pass_context
def stale_alert(ctx, min_age_days):
    """已修复未 resolve 旧 alert 清单 — 逐条附健康证据, 供统筹裁定 (本命令不写库)"""
    started = time.time()
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(started, "DB_ERROR", f"数据库连接失败: {e}",
                          command="issues stale-alerts"), 1)
        return
    try:
        rows = conn.execute(
            "SELECT id, severity, layer, title, detail, created_at FROM alerts "
            "WHERE status = 'open' AND created_at < datetime('now', ?) "
            "ORDER BY created_at", (f"-{int(min_age_days)} days",)).fetchall()
        entries = []
        for r in rows:
            ev = _alert_health_evidence(conn, r["title"], r["created_at"])
            entries.append({
                "alert_id": r["id"], "severity": r["severity"], "layer": r["layer"],
                "title_zh": r["title"], "created_at": r["created_at"],
                "health_evidence": ev,
                "recommendation_zh": (
                    "证据显示已恢复, 建议 resolve (待统筹裁定)"
                    if ev.get("healthy") else
                    "证据不足或仍异常, 保留观察" if ev.get("healthy") is None
                    else "告警后仍有失败/仍红, 勿 resolve")})
    finally:
        conn.close()
    env = _envelope(started, _today(), [], {}, command="issues stale-alerts")
    env.pop("issues")
    env["stale_alerts"] = entries
    env["summary"] = {"total": len(entries),
                      "likely_resolvable": sum(1 for e in entries
                                               if e["health_evidence"].get("healthy"))}
    human = [f"🗂 已修复未 resolve 旧 alert 清单 — 共 {len(entries)} 条 "
             f"(证据支持 resolve: {env['summary']['likely_resolvable']} 条)"]
    for e in entries:
        mark = "✅可resolve" if e["health_evidence"].get("healthy") else (
            "❓待查" if e["health_evidence"].get("healthy") is None else "❌勿动")
        human.append(f"\n#{e['alert_id']} [{e['severity']}] {e['title_zh']} "
                     f"({e['created_at']}) → {mark}")
        human.append(f"   证据: {json.dumps(e['health_evidence'], ensure_ascii=False)}")
        human.append(f"   建议: {e['recommendation_zh']}")
    env["_human"] = "\n".join(human)
    _emit(ctx, env, 0)
