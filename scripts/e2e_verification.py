"""Sprint 3 端到端链路验证脚本。

用法:
  PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/e2e_verification.py --date 2026-08-14
  PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/e2e_verification.py --phase pre   # 盘前
  PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/e2e_verification.py --phase post  # 盘后
  PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/e2e_verification.py --phase full  # 全天

输出: logs/e2e_verification.jsonl (每行一条记录)
       stdout 人类可读汇总
退出码: 0 = 全绿 / 1 = 有失败 / 2 = 部分失败(警告)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from ops_layer import OpsLayer

# 盘前链路管道 (08:00-09:45 应已运行)
PRE_MARKET_PIPELINES = [
    "pre_market_refresh",
    "auction_parse",
    "morning_review",
]

# 盘后链路管道 (16:00-18:00 应已运行)
POST_MARKET_PIPELINES = [
    "end_of_day",
    "daily_data_pipeline",
    "factor_update",
    "ic_feedback",
    "daily_backtest",
    "evening_report",
    "daily_ops_report",
    "db_backup",
]

# 以脚本方式调度(非 pipeline)的报告，通过日志文件验证
SCRIPT_REPORTS = [
    ("morning_brief", "morning_brief.log", "晨报"),
]

MIN_KLINE_ROWS = 4000

# B1 (2026-10-21): DDP「有效成功」判据 — A2a 契约
# (qs-handoff/DDP_CONTRACT_20260920.md)。
# critical = {kline, valuation} (DAILY_SUB_PIPELINES - best_effort)。
# tolerated = orchestrator best_effort 白名单 11 腿 (与
# src/ops_layer/orchestrator.py:871-874 同源, 快照非 import — 脚本层不跨层
# import 执行层, 漂移由测试钉锁)。
# 边界: 只对 pipe == "daily_data_pipeline" 生效; 其余管道裸判定不变;
# failed 永不豁免; 降级须全具名且全在白名单内。
DDP_CRITICAL_LEGS = ("kline", "valuation")
DDP_TOLERATED_LEGS = ("stock_list", "moneyflow", "lhb_daily",
                      "analyst_forecasts", "concept_daily",
                      "market_sentiment", "stock_news",
                      "stock_limit_up_down", "shortterm_collect",
                      "index_daily", "fuyao_financial")


def _ddp_effective_success(conn, error_msg, target_date):
    """DDP partial 是否算有效成功。返回 (ok, reason)。

    三闸全过才 pass:
    ① error_msg 里出现的腿名全在 tolerated 白名单内具名
      (critical 腿名出现即 fail; 白名单外腿名出现即 fail)。
    ② kline 当日覆盖率 >= MIN_KLINE_ROWS (直查 daily_klines)。
    """
    import re
    err = error_msg or ""
    # G1 修 (2026-10-22): 原正则 `([a-z][a-z0-9_]*)\s*:` 会连**括号内的子原因**一起抓。
    # 实证: "shortterm_collect: partial (anomaly: 无数据 ...)" 里的 `anomaly` 是
    # shortterm_collect 的内部原因, 不是顶层腿 —— 原实现把它判成「非白名单腿降级」,
    # 导致 e2e 夜间 10 连败。现只在**括号深度 0** 的位置取 `xxx:` 的 xxx。
    legs = []
    depth = 0
    i = 0
    while i < len(err):
        ch = err[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            # 回看冒号前的标识符
            m = re.search(r"([a-z][a-z0-9_]*)\s*$", err[:i])
            if m:
                legs.append(m.group(1))
        i += 1
    if not legs:
        return False, "partial 但降级未具名腿 (effective_success 拒识)"
    for leg in legs:
        if leg in DDP_CRITICAL_LEGS:
            return False, f"critical 腿降级: {leg}"
        if leg not in DDP_TOLERATED_LEGS:
            return False, f"非白名单腿降级: {leg}"
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE date = ?",
            (target_date,),
        ).fetchone()
        cnt = 0
        if row is not None:
            try:
                cnt = row[0] or 0
            except (KeyError, IndexError, TypeError):
                vals = list(row.values()) if hasattr(row, "values") else []
                cnt = (vals[0] or 0) if vals else 0
    except Exception as exc:
        return False, f"kline 覆盖率不可查: {exc}"
    if cnt < MIN_KLINE_ROWS:
        return False, f"kline 覆盖率不足: {cnt} < {MIN_KLINE_ROWS}"
    return True, "有效成功: critical 全过+kline=%d+降级全具名tolerated" % cnt

# 这些管道运行时间晚于 e2e full 检查(18:00), 检查"最近一次成功运行够近"即可,
# 而非强求当天有记录。db_backup cron 在 23:00, 18:00 时当天必然无记录 -> 旧逻辑误报。
# 卡 fix/e2e-backup-tradingday-window (2026-10-20, 派单 sm-3ea29070027b4ee4):
# 窗口按**交易日**伸缩, 不按自然日 — 10-19(周一) 检查时最近备份 10-16(周五),
# 自然日差 3, 但 10-17/18/19 周末+周日 cron 不跑备份, 自然日差把正常周末
# 间隔误判成超窗 (生产 e2e full 10-19 fail 实证)。周末/假日不计入。
LATE_PIPELINES = {"db_backup"}
LATE_PIPELINE_WINDOW_DAYS = 1  # 允许最近 1 个交易日(含上一交易日)有成功运行


def _trading_days_between(from_date: str, to_date: str) -> int | None:
    """(from_date, to_date] 之间的交易日数 — 周末/假日不计。

    日历面 TradingCalendar (静态 holiday 表 + 周末), 不读 daily_klines
    (库停更=病源, 拿它当锚=用有病产物当基线, 同 readiness
    prev_trading_day_before v4 注释)。坏格式/异常 → None (调用方保守 fail)。
    """
    try:
        from common.calendar import TradingCalendar
        d = datetime.strptime(from_date, "%Y-%m-%d").date()
        t = datetime.strptime(to_date, "%Y-%m-%d").date()
        if t <= d:
            return 0
        cal = TradingCalendar()
        n = 0
        cur = d
        while cur < t:
            cur += timedelta(days=1)
            if cal.is_trading_day(cur.strftime("%Y-%m-%d")):
                n += 1
            if n > 400:  # 防御: 日历库空/坏数据时不得死循环
                return None
        return n
    except Exception:
        return None


def _today_cn() -> str:
    now = datetime.now()
    if now.weekday() >= 5:
        return (now - timedelta(days=now.weekday() - 4)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def _write_evidence(record: dict) -> None:
    record.setdefault("timestamp", datetime.now().isoformat())
    with open("logs/e2e_verification.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def check_pipelines(app: OpsLayer, target_date: str, pipelines: list[str]) -> list[dict]:
    checks = []
    conn = app.db.get_connection()
    rows = conn.execute(
        "SELECT pipeline_name, status, error_msg, duration_s FROM pipeline_runs "
        "WHERE date = ?",
        (target_date,),
    ).fetchall()
    by_name: dict[str, dict] = {r["pipeline_name"]: dict(r) for r in rows}
    # 预查"最近一次运行"用于晚于本次检查的管道(如 db_backup cron 23:00 > full 检查 18:00)
    latest_runs: dict[str, dict] = {}
    if LATE_PIPELINES.intersection(pipelines):
        lr = conn.execute(
            "SELECT pipeline_name, date, status, error_msg, duration_s "
            "FROM pipeline_runs "
            "WHERE pipeline_name IN (" + ",".join("?" for _ in LATE_PIPELINES) + ") "
            "ORDER BY rowid DESC",
            tuple(LATE_PIPELINES),
        ).fetchall()
        for r in lr:
            nm = r["pipeline_name"]
            if nm not in latest_runs:
                latest_runs[nm] = dict(r)
    for pipe in pipelines:
        rec = by_name.get(pipe)
        if rec is None:
            if pipe in LATE_PIPELINES:
                # 当天无记录但该管道晚于本次检查运行: 改查最近窗口内是否有成功运行
                latest = latest_runs.get(pipe)
                if latest:
                    # 交易日窗口 (fix/e2e-backup-tradingday-window): 周末/假日
                    # 不计 — 10-17/18(周末)/19(周一) cron 不跑备份是正常间隔,
                    # 自然日差 3 误报超窗 (10-19 生产 e2e full fail 实证)。
                    gap = _trading_days_between(latest["date"], target_date)
                    within = (gap is not None
                              and gap <= LATE_PIPELINE_WINDOW_DAYS)
                    ok = latest["status"] == "success" and within
                    gap_desc = (f"交易日差 {gap}" if gap is not None
                                else "交易日差不可算 (日历异常)")
                    checks.append({
                        "name": pipe,
                        "status": "pass" if ok else "fail",
                        "detail": (f"当天无记录, 最近一次 {latest['date']} "
                                   f"status={latest['status']} ({gap_desc})"
                                   if within else
                                   f"最近备份 {latest['date']} 超出窗口 "
                                   f"({gap_desc} > {LATE_PIPELINE_WINDOW_DAYS}) "
                                   f"status={latest['status']}"),
                    })
                else:
                    checks.append({"name": pipe, "status": "fail", "detail": "无运行记录"})
                continue
            checks.append({"name": pipe, "status": "fail", "detail": "无运行记录"})
            continue
        if pipe == "daily_data_pipeline" and rec["status"] == "partial":
            # B1: DDP partial 按 A2a 有效成功判 (其余管道裸判定不动)。
            ok, why = _ddp_effective_success(
                conn, rec["error_msg"], target_date)
            checks.append({
                "name": pipe,
                "status": "pass" if ok else "fail",
        "detail": ("status=partial dur={}s effective_success={} ({}) "
                  "err={}").format(rec["duration_s"], ok, why,
                                  rec["error_msg"] or "-"),
            })
            continue
        ok = rec["status"] == "success"
        checks.append({
            "name": pipe,
            "status": "pass" if ok else "fail",
            "detail": f"status={rec['status']} dur={rec['duration_s']}s err={rec['error_msg'] or '-'}",
        })
    return checks


def check_data_freshness(app: OpsLayer, target_date: str) -> list[dict]:
    checks = []
    conn = app.db.get_connection()
    for table in ["daily_klines", "factor_daily", "model_predictions"]:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE date = ?", (target_date,)
            ).fetchone()
            cnt = row[0] or 0
            tgt = MIN_KLINE_ROWS if table == "daily_klines" else 1
            checks.append({
                "name": table,
                "status": "pass" if cnt >= tgt else "fail",
                "detail": f"{target_date} 行数={cnt}",
            })
        except Exception as exc:
            checks.append({"name": table, "status": "fail", "detail": f"查询异常: {exc}"})
    return checks


def check_logs(target_date: str) -> list[dict]:
    checks = []
    for name, path, label in SCRIPT_REPORTS:
        checks.append(_check_log_file(path, target_date, label))
    return [c for c in checks if c]


def _check_log_file(path: str, target_date: str, label: str) -> dict | None:
    import os

    full = os.path.join("logs", path)
    if not os.path.exists(full):
        return {"name": label, "status": "warn", "detail": f"{path} 不存在"}
    with open(full, encoding="utf-8", errors="ignore") as fh:
        content = fh.read()
    if target_date in content:
        return {"name": label, "status": "pass", "detail": f"{path} 含 {target_date}"}
    return {"name": label, "status": "warn", "detail": f"{path} 无目标日期记录"}


def _is_trading_day(target_date: str, db=None) -> bool:
    """B2 (2026-10-21): 判定目标日期是否交易日。

    crontab ``30 20 * * 1-5`` 只排周末不排节假日, 10-01/02/05/06/07 这类
    法定假日仍会触发 e2e 并跑出一堆「管道无运行记录」的 fail = 假日假红,
    污染 e2e 历史与告警。入口处先问日历, 非交易日直接 skip。

    日历不可用 (导入失败/无 db) 时保守返回 True —— 宁可照常跑判据,
    也不因为日历挂了就把真正的交易日跳过 (漏检比误报更坏)。
    """
    try:
        from common.calendar import TradingCalendar
        return bool(TradingCalendar(db=db).is_trading_day(target_date))
    except Exception as e:  # noqa: BLE001 —— 日历不可用不阻断验证
        print(f"[e2e] 交易日判定失败, 按交易日处理: {e}")
        return True


def run(target_date: str, phase: str, notify_disabled: bool = False) -> int:
    app = OpsLayer()
    try:
        # B2 (2026-10-21) 假日守卫: 非交易日不跑判据, 直接 success-skip 留痕。
        # 判据面零改动 —— 只是不再在休市日制造假红。
        if not _is_trading_day(target_date, db=getattr(app, "db", None)):
            skip_reason = f"{target_date} 非交易日 (TradingCalendar), 跳过验证"
            print(f"Sprint3 验证 [{phase}] {target_date}: SKIP — {skip_reason}")
            _write_evidence({
                "event": "e2e_verification",
                "phase": phase,
                "date": target_date,
                "skipped": True,
                "skip_reason": skip_reason,
                "total": 0, "fails": 0, "warns": 0,
                "checks": [],
            })
            return 0

        all_checks: list[dict] = []
        if phase == "pre":
            all_checks += check_pipelines(app, target_date, PRE_MARKET_PIPELINES)
            all_checks += check_logs(target_date)
        elif phase == "post":
            all_checks += check_pipelines(app, target_date, POST_MARKET_PIPELINES)
            all_checks += check_data_freshness(app, target_date)
        elif phase == "full":
            all_checks += check_pipelines(app, target_date, PRE_MARKET_PIPELINES)
            all_checks += check_pipelines(app, target_date, POST_MARKET_PIPELINES)
            all_checks += check_data_freshness(app, target_date)
            all_checks += check_logs(target_date)

        fails = [c for c in all_checks if c["status"] == "fail"]
        warns = [c for c in all_checks if c["status"] == "warn"]
        ec = 0 if not fails else 1
        summary = (
            f"Sprint3 验证 [{phase}] {target_date}: "
            f"{len(all_checks)} 项, {len(fails)} 失败, {len(warns)} 警告"
        )
        print(summary)
        for c in all_checks:
            mark = {"pass": "✅", "fail": "❌", "warn": "⚠️"}[c["status"]]
            print(f"  {mark} {c['name']}: {c['detail']}")

        _write_evidence({
            "event": "e2e_verification",
            "phase": phase,
            "date": target_date,
            "total": len(all_checks),
            "fails": len(fails),
            "warns": len(warns),
            "checks": all_checks,
        })

        if not notify_disabled:
            _push_notification(target_date, phase, all_checks)
        return ec
    finally:
        app.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_today_cn())
    parser.add_argument("--phase", choices=["pre", "post", "full"], default="full")
    parser.add_argument("--no-notify", action="store_true", help="验证后不推送飞书通知")
    args = parser.parse_args()
    return run(args.date, args.phase, notify_disabled=args.no_notify)


def _push_notification(target_date: str, phase: str, all_checks: list[dict]) -> None:
    """验证完成后推送飞书 DM 通知用户。"""
    import os

    try:
        from dotenv import load_dotenv

        from config_layer import Config
        from delivery_layer import DeliveryLayer, Message
        from report_layer.formatters.feishu_card import FeishuCardFormatter

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        load_dotenv(os.path.join(project_root, ".env"))

        fails = [c for c in all_checks if c["status"] == "fail"]
        warns = [c for c in all_checks if c["status"] == "warn"]
        phase_label = {"pre": "盘前", "post": "盘中", "full": "全天"}[phase]
        overall = "green" if not fails else "red"
        emoji = {"green": "✅", "red": "❌"}.get(overall, "⚠️")
        lines = [
            f"📋 Sprint3 验证 {phase_label}",
            f"日期: {target_date}",
            f"状态: {emoji} {'通过' if overall == 'green' else '有失败'} ",
            f"检查: {len(all_checks)} 项 | 失败 {len(fails)} | 警告 {len(warns)}",
        ]
        for c in all_checks:
            mark = {"pass": "✅", "fail": "❌", "warn": "⚠️"}[c["status"]]
            lines.append(f"{mark} {c['name']}: {c['detail']}")
        content = {"sections": [{"title": f"Sprint3 验证 {phase_label}", "body": "\n".join(lines)}]}
        config = Config(); config.load()
        msg = Message(content=content, content_type="e2e_verification",
                      title=f"📥 Sprint3 验证 {phase_label} — {target_date}")
        msg.content = FeishuCardFormatter().format(msg)
        dl = DeliveryLayer(config=config)
        dl.registry.mark_healthy("feishu_dm")
        results = dl.send(msg)
        if not isinstance(results, list):
            results = [results]
        for r in results:
            print(f"推送 [{getattr(r, 'channel', '?')}]: {getattr(r, 'status', '?')}")
    except Exception as exc:
        print(f"⚠️ 飞书通知推送失败: {exc}")


if __name__ == "__main__":
    # F6.1: 哨兵包装 — 崩溃/失败写 pipeline_runs; 本脚本自带飞书通知,
    # 关闭哨兵的非零退出码告警避免重复推送 (崩溃异常仍会告警)
    from ops_layer.run_guard import run_guarded

    sys.exit(run_guarded("e2e_verification", main, alert_on_exit_code=False))
