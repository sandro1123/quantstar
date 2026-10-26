"""ops_layer/heartbeat_check.py — P0-3①: healthchecks 式心跳模型 (超期未打点=主动告警)

治"静默失败": 管道崩溃在落 pipeline_runs 之前 (CF-01 型 dotenv 崩溃/入口脚本
被杀/cron 行漂移) 时, 失败告警链路本身也死了 — 只有"应到而未到"的主动核对
才能发现。与既有资产分工:
- cron_watchdog: 进程级预算 (跑起来但超时/被杀)
- G4 饿死告警:   依赖门 skip 留痕 (跑起来但被依赖挡住)
- freshness_audit: 表级数据新鲜度 (跑起来且落了库但数据不全)
- 本模块:        排程级心跳 (根本没跑起来/没成功 — 以上四者的共同盲区)

心跳打点零新写入路径: orchestrator 落 pipeline_runs 的 success/partial 行
就是心跳本身 (healthchecks.io 的 "check-in" 语义)。本模块只读核对:
    deadline = schedule + 预算 + grace
    预算 = cron_watchdog.sh 的 DL-65 统一公式 (ceiling/依赖等待窗, 同源复刻)
    now > deadline 且当日无 success/partial 行 → overdue → AlertManager 告警
告警去重走 P0-3② 状态机 (持续期不重复轰炸); 恢复走既有
resolve_pipeline_alerts (管道补跑成功自动关闭 + 推 recovery)。
"""

from __future__ import annotations

from datetime import datetime

from common.logging import get_logger

logger = get_logger("ops.heartbeat_check")

DEFAULT_GRACE_S = 1800          # 排程时刻后的基础宽限 (30min)
from ops_layer.budget_formula import compute_ceiling  # 单一定义点 (判例22)
DEP_MARGIN_S = 60               # 与 cron_watchdog.sh DL-65 同源
_HEARTBEAT_TITLE = "心跳超期: "  # 措辞即协议: resolve_pipeline_alerts 按管道名匹配

# ── 丁 (2026-09-25 分诊): 心跳判据不适用的入口登记表 ──────────────────
# 有排程、真在跑、跑成功, 但**不产 pipeline_runs 载体**的入口。原样纳入
# pipeline_runs 判据会永久误报。统筹裁定: 「豁免清单本身就是文档, 不是黑洞」
# —— 必须显式登记并写明替代判据, 不得静默豁免。
NO_PIPELINE_RUNS_ENTRIES = {
    "dashboard_bitable_sync": (
        "crontab 走 cron_watchdog.sh 直驱 (非 `pipeline run`), 不落 pipeline_runs。"
        "生产实证 09-22/23/24 每天 watchdog_start → watchdog_done rc=0 (约 25s 跑完), "
        "但 pipeline_runs 从无一行。替代判据: logs/watchdog.jsonl 的 "
        "watchdog_done rc=0 (当日存在即算成功)。"
    ),
    "e2e_verification": (
        "scripts 直驱 (scripts/e2e_verification.py --phase pre|full), 不产 "
        "pipeline_runs 载体。"
        "生产实证: 晚间档 (20:30 --phase full) 09-15~09-24 连续 failed exit=1, "
        "白天档 (09:45 --phase pre) 全 success。**但晚间那条红得对** —— "
        "读日志确认: 它按设计在「有项失败」时 exit 1, 且它报的正是 DDP 的 "
        "`kline coverage insufficient: 1909 rows` (估值断供), 是**唯一如实报 "
        "DDP 失败的哨兵**。故不得按 pipeline_runs 判据处理 (否则会被当成常年红)。"
        "替代判据: 日志尾部 `Sprint3 验证 [phase] DATE: N 项, M 失败` 行。"
        "⚠️ 当前推送载体 = Feishu DM (日志 `推送 [feishu_dm]: success`), "
        "**无人值守确认** —— DM 没人看则该链是断的。心跳接线后载体改为 "
        "alerts 行; 此处先登记以不断链 (判例: 豁免清单即文档, 不是黑洞)。"
    ),
}


def _days_match(days_spec, dt) -> bool:
    """甲 (2026-09-25): cron 风格 days 匹配。

    原实现完全忽略 days → `weekly_summary(days='6')` / `marimo_weekly_report('6')` /
    `wfo_gate('0')` **每天**被报超期 = 每天 3 条铁定假阳 (回溯实证)。
    语义: cron dow 0=周日..6=周六; 缺省/'*' = 每天。
    """
    if days_spec in (None, "", "*"):
        return True
    dow = (dt.weekday() + 1) % 7          # python Mon=0 → cron Mon=1, Sun=0
    for part in str(days_spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                if int(a) <= dow <= int(b):
                    return True
            except ValueError:
                continue
        elif part.isdigit() and int(part) == dow:
            return True
    return False


def schedule_entries_from_crontab(crontab_text: str) -> dict:
    """乙 (2026-09-25): 从 crontab 同源取排程元数据。

    统筹裁定④: **走 crontab 同源, 不在 ops.yaml 另补声明** —— 另补一份 =
    新的漂移源 (单一定义点)。

    治的病: 原监控集只有 `ops.pipelines`(51条), 而「scripts/* 直驱」入口
    (daily_update / intraday_scan / e2e_verification / api_health_check) 住在
    `ops.cron_watchdog.overrides`, 心跳天然看不见 —— 而 09-21~24 连续四天
    锁饿死的恰好就是这一类。

    顺带收益: ops.yaml 里有 schedule 但 crontab 无行的"排程漂移"条目
    (tail_advisory / marimo_daily_report) 自动不在本集内, 不再产生假阳。

    返回 {key: {"schedule": "HH:MM", "days": "<cron dow>"}}。
    """
    import re

    out: dict[str, dict] = {}
    for line in (crontab_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        minute, hour, dow, body = m.groups()
        if not (minute.isdigit() and hour.isdigit()):
            continue          # */5 之类高频项无"应到"概念, 不监控
        keys = re.findall(
            r"(?:flock_guard\.sh|cron_watchdog\.sh|pipeline\s+run)\s+([A-Za-z_]\w*)",
            body)
        m2 = re.search(r"-m\s+src\.execution_layer\.scripts\.(\w+)", body)
        if m2:
            keys.append(m2.group(1))
        for k in keys:
            out.setdefault(k, {"schedule": f"{hour.zfill(2)}:{minute.zfill(2)}",
                               "days": dow})
    return out


def orphan_override_keys(overrides: dict, pipelines: dict) -> list[str]:
    """P1-1 (09-28): overrides 键集 − pipelines 键集 = 孤儿键 (注册面漂移)。

    治的病: cron_watchdog.overrides 登记的腿若不在 ops.pipelines, budget 派生
    if key in pipelines 分支翻转, 裸加靠 default 巧合=300 — 两注册面漂移无显形。
    判例=「有注释」≠「有约束」: 不静默靠 default, 孤儿键显式列出。
    """
    return sorted(set(overrides or {}) - set(pipelines or {}))


def _parse_schedule(schedule: str) -> int | None:
    """'HH:MM' → 当日分钟数; 非法/非时刻表 (continuous 等) → None (不监控)。"""
    try:
        hh, mm = str(schedule).strip().split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def budget_s(pcfg: dict) -> int:
    """执行预算 — 薄壳转投单一定义点 (ops_layer.budget_formula).

    DL-65 公式原双实现已收口 (夜班 09-11 判例 22): cron_watchdog.sh 与
    本模块与 zombie reaper 三消费面共用 budget_formula.compute_ceiling,
    改公式必须三处同改 — 漂移即误杀 (ddp 3000s<5550s 病历)。
    """
    return compute_ceiling(pcfg)


def check(pipelines: dict, runs: list[dict], now: datetime,
          hb_cfg: dict | None = None,
          prior_runs_by_day: dict | None = None,
          min_consecutive: int = 1) -> list[dict]:
    """纯逻辑核心: 返回 overdue 清单 [{name, schedule, deadline, overdue_min}].

    pipelines: ops.yaml 的 ops.pipelines; runs: 当日 pipeline_runs 行
    (dict 化, 需 pipeline_name/status); hb_cfg: ops.heartbeat
    (grace_s 默认 + overrides.<name> 每管道宽限覆盖)。
    """
    hb_cfg = hb_cfg or {}
    grace_default = int(hb_cfg.get("grace_s", DEFAULT_GRACE_S))
    overrides = hb_cfg.get("overrides", {}) or {}
    # P1-1: 注册面对账 — overrides 有 pipelines 无 = 孤儿键, 显式 warning 不静默
    _orphans = orphan_override_keys(overrides, pipelines)
    if _orphans:
        logger.warning(
            f"heartbeat: overrides 孤儿键 (在 overrides 不在 pipelines, "
            f"budget 靠 default 巧合): {_orphans}")
    overdue = []
    for name, pcfg in (pipelines or {}).items():
        sched_min = _parse_schedule((pcfg or {}).get("schedule", ""))
        if sched_min is None:
            continue  # continuous/无时刻表: 无"应到"概念, 不监控
        # 丁: 载体缺失类不走 pipeline_runs 判据 (否则永久误报); 登记表即文档
        if name in NO_PIPELINE_RUNS_ENTRIES:
            continue
        # 甲: 非其排程日不判 (周频腿天天报 = 必假阳)
        if not _days_match((pcfg or {}).get("days"), now):
            continue
        grace = int(overrides.get(name, grace_default))
        deadline_min = sched_min + (budget_s(pcfg or {}) + grace) / 60.0
        now_min = now.hour * 60 + now.minute + now.second / 60.0
        if now_min <= deadline_min:
            continue  # 未到期 (含跑批中): 不打扰
        done = any(r.get("pipeline_name") == name
                   and r.get("status") in ("success", "partial")
                   for r in runs)
        # 丙: 「连续 min_consecutive 个**交易日**未成功」才算故障。
        # 单日迟到不是故障 (分诊实证: 存量 18 条里 13 条是单日迟到)。
        # 「交易日」口径由调用方保证 —— prior_runs_by_day 的键即前若干个交易日,
        # 周末天然不在其中, 故周五红 + 周一红 = 连续 2 天 (统筹钉①)。
        if not done and min_consecutive > 1 and prior_runs_by_day:
            prior_days = sorted(prior_runs_by_day, reverse=True)[:min_consecutive - 1]
            recovered = any(
                r.get("pipeline_name") == name
                and r.get("status") in ("success", "partial")
                for d in prior_days
                for r in (prior_runs_by_day.get(d) or [])
            )
            if recovered:
                continue   # 之前若干交易日成功过 → 只是今天迟到, 不报
        if not done:
            overdue.append({
                "name": name,
                "schedule": (pcfg or {}).get("schedule", ""),
                "deadline": f"{int(deadline_min // 60):02d}:{int(deadline_min % 60):02d}",
                "overdue_min": int(now_min - deadline_min),
            })
    return overdue


def check_and_alert(db, pipelines: dict, hb_cfg: dict | None = None,
                    now: datetime | None = None) -> list[dict]:
    """薄接线: 读当日 pipeline_runs → check → 超期走 AlertManager (状态机去重)。"""
    from ops_layer.alert_manager import AlertManager

    now = now or datetime.now()
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT pipeline_name, status FROM pipeline_runs WHERE date = ?",
        (now.strftime("%Y-%m-%d"),),
    ).fetchall()
    runs = [dict(r) for r in rows]
    overdue = check(pipelines, runs, now, hb_cfg)
    if not overdue:
        return []
    am = AlertManager(db)
    for item in overdue:
        try:
            am.create_alert(
                title=f"{_HEARTBEAT_TITLE}{item['name']}",
                detail=(f"管道 {item['name']} 排程 {item['schedule']}, 截止 "
                        f"{item['deadline']} (预算+宽限), 已超期 "
                        f"{item['overdue_min']} 分钟仍无当日成功记录。\n"
                        f"可能: 入口崩溃在落 pipeline_runs 之前 (CF-01 型) / "
                        f"cron 行漂移 / 进程被杀。\n"
                        f"排查: logs/watchdog.jsonl 与 logs/pipeline.log 找该 key。"),
                severity="warning", layer="ops",
            )
        except Exception as e:  # noqa: BLE001 — 单条告警失败不阻断其余
            logger.warning(f"heartbeat alert failed for {item['name']}: {e}")
    return overdue


def main() -> int:
    """CLI: python -m ops_layer.heartbeat_check [--yaml PATH]

    退出码: 0=无超期, 1=有超期 (已告警), 2=配置读取失败。
    """
    import argparse
    import os
    from pathlib import Path

    import yaml

    parser = argparse.ArgumentParser(description="QS 管道心跳核对 (超期未成功=主动告警)")
    default_yaml = Path(__file__).resolve().parents[2] / "config" / "defaults" / "ops.yaml"
    parser.add_argument("--yaml", default=os.environ.get("QS_OPS_YAML", str(default_yaml)))
    parser.add_argument("--now", default=None,
                        help='固定核对时刻 "YYYY-MM-DD HH:MM" (默认当前时刻; 测试/事后核对用)')
    args = parser.parse_args()
    now_override = None
    if args.now:
        try:
            now_override = datetime.strptime(args.now, "%Y-%m-%d %H:%M")
        except ValueError:
            print(f"[heartbeat_check] --now 格式非法: {args.now!r} (需 'YYYY-MM-DD HH:MM')")
            return 2
    try:
        with open(args.yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[heartbeat_check] 无法读取 {args.yaml}: {e}")
        return 2
    ops = cfg.get("ops", {}) or {}
    pipelines = ops.get("pipelines", {}) or {}
    hb_cfg = ops.get("heartbeat", {}) or {}

    from common.database import Database
    db_path = os.environ.get("QS_DB_PATH",
                             str(Path(__file__).resolve().parents[2] / "data" / "qs.db"))
    db = Database(db_path)
    try:
        overdue = check_and_alert(db, pipelines, hb_cfg, now=now_override)
    finally:
        db.close()
    if overdue:
        names = ", ".join(o["name"] for o in overdue)
        print(f"[heartbeat_check] OVERDUE {len(overdue)}: {names}")
        return 1
    print("[heartbeat_check] OK: 无超期管道")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
