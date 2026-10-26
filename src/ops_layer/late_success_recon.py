"""ddp 两小卡② — late_success 对账: pipeline_runs 状态 vs 实际落库自检

背景: 08-26 补跑链 9 作业全 success@aa25e99 是一次性人工实证, 无可持续
自检 — status='success' 是否真的落了库, 没人再核过。本模块让对账常态化:

口径 (late_success): 同一 (date, pipeline_name) 上, 失败/跳过/部分失败之后
又出现 success — 补跑链的典型形态。对账 = 该 success 行声称的数据足迹
在业务表里真实存在且日期对齐。

设计 (诚实对账, 铁律 #12):
- 只读: mode=ro 连接, 不写任何表; 结论只落 stdout/JSON;
- FOOTPRINTS 显式映射: 管道 → 落库表 + 日期列。未知管道 (映射缺) 不会
  被静默判 OK — 标 verdict='unverified', 明示"对账未覆盖"而非装作通过;
- 宁 NULL 不假值: 足迹缺失 → verdict='mismatch', 绝不拿"status 写了
  success"冒充"数据真落了";
- 表缺失/查询异常 → verdict='error', 不静默吞错。

用法:
  python -m src --json recon late-success [--date 2026-08-26] [--pipeline X]
  不带 --json 人类可读中文。
"""
import json
import sqlite3
from datetime import datetime

from common.logging import get_logger

logger = get_logger("ops_layer.late_success_recon")

# success 之后的非成功终态 (排除 running — 它是未终态不是反证)
_BAD_PRIOR = ("failed", "skipped", "partial")

# 管道 → 落库足迹: (表名, 日期列)。
# 只登记"该管道 success 必然留下当日行"的核心表; 足迹在多表的管道
# (如 daily_data_pipeline 聚合多个子管道) 取最核心一张 — 对账口径宁少
# 勿滥, 没把握的表不入映射 (入即未登记 → unverified, 不假 OK)。
FOOTPRINTS: dict[str, tuple[str, str]] = {
    "kline": ("daily_klines", "date"),
    "index_daily": ("index_daily", "date"),
    "factor_update": ("factor_daily", "date"),
    "adjust_factor": ("adjust_factors", "date"),
    "moneyflow": ("stock_moneyflow", "date"),
    "fwd_return_backfill": ("factor_daily", "date"),
    "daily_backtest": ("backtest_runs", "date_range_start"),
    "quality_check": ("quality_checks", "date"),
    "freshness_audit": ("freshness_audit", "date"),
    "concept": ("concept_daily", "date"),
}


def _connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)


def _resolve_db() -> str:
    import os
    p = os.environ.get("QS_TEST_DB", "")
    if p:
        return p
    from config_layer import cfg_get
    root = cfg_get("data.db_path", "data/qs.db")
    return str(root) if str(root).startswith("/") else root


def find_late_success(conn: sqlite3.Connection, date: str,
                      pipeline: str = "") -> list[dict]:
    """找 late_success: 同 (date, pipeline) 上坏终态在前 success 在后。

    返回按 (pipeline_name, created_at) 排序; 同管道多次坏→success 各算一条
    success 行 (补跑链可能反复, 每条 success 都该被核)。
    """
    sql = """
        SELECT s.run_id, s.pipeline_name, s.date, s.created_at,
               (SELECT GROUP_CONCAT(p.status)
                  FROM pipeline_runs p
                 WHERE p.date = s.date AND p.pipeline_name = s.pipeline_name
                   AND p.created_at < s.created_at
                   AND p.status IN ('failed','skipped','partial')) AS priors
          FROM pipeline_runs s
         WHERE s.date = ? AND s.status = 'success'
    """
    args: list = [date]
    if pipeline:
        sql += " AND s.pipeline_name = ?"
        args.append(pipeline)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for run_id, name, d, created, priors in rows:
        if priors:  # 前面有坏终态 → 这是补跑后的 success
            out.append({"run_id": run_id, "pipeline_name": name,
                        "date": d, "created_at": created,
                        "prior_statuses": priors.split(",")})
    return out


def recon_one(conn: sqlite3.Connection, item: dict) -> dict:
    """核一条 late_success: FOOTPRINTS 足迹在业务表真实存在。"""
    name = item["pipeline_name"]
    if name not in FOOTPRINTS:
        return {**item, "verdict": "unverified",
                "footprint": None,
                "reason_zh": f"管道 {name} 未登记落库足迹映射 — 对账未覆盖, "
                             f"不判 OK (加映射后再核)"}
    table, date_col = FOOTPRINTS[name]
    try:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?",
            (item["date"],)).fetchone()[0]
    except sqlite3.OperationalError as e:
        return {**item, "verdict": "error", "footprint": f"{table}.{date_col}",
                "reason_zh": f"足迹查询失败: {str(e)[:120]}"}
    return {**item, "verdict": "ok" if n > 0 else "mismatch",
            "footprint": f"{table}.{date_col}", "footprint_rows": n,
            "reason_zh": (f"{table} 当日 {n} 行, 与 success 状态一致" if n > 0
                          else f"{table} 当日 0 行 — status=success 但数据"
                               f"未落库, 需人工核查")}


def run_recon(db_path: str, date: str, pipeline: str = "") -> dict:
    """对账主入口。返回 {summary, late_success, verdict_counts}。"""
    conn = _connect_ro(db_path)
    try:
        items = find_late_success(conn, date, pipeline)
        results = [recon_one(conn, it) for it in items]
        counts: dict[str, int] = {}
        for r in results:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        ok = all(r["verdict"] in ("ok",) for r in results) if results else True
        return {"date": date, "total": len(results),
                "verdict_counts": counts,
                "recon_ok": ok,
                "late_success": results}
    finally:
        conn.close()


def _main_human(result: dict) -> None:
    print(f"late_success 对账 {result['date']}: {result['total']} 条补跑链 "
          f"success 待核")
    for r in result["late_success"]:
        mark = {"ok": "✓", "mismatch": "✗", "error": "!",
                "unverified": "?"}[r["verdict"]]
        print(f"  [{mark}] {r['pipeline_name']} run={r['run_id'][:24]} "
              f"足迹={r.get('footprint') or '-'} {r['reason_zh']}")


def main(db_path: str = "", date: str = "", pipeline: str = "",
         as_json: bool = False) -> int:
    """CLI 入口: python -m src --json recon late-success [--date ...]"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    result = run_recon(db_path or _resolve_db(), date, pipeline)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _main_human(result)
    # exit 0=对账通过或无待核; 1=存在 mismatch/error (有假 success 嫌疑)
    return 0 if (result["recon_ok"] and
                 not result["verdict_counts"].get("mismatch") and
                 not result["verdict_counts"].get("error")) else 1
