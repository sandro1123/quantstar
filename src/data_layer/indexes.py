"""indexes.py — DL-16 二级索引 (按实际查询模式, EXPLAIN QUERY PLAN 验证)

F-14: snapshot()/因子查询按 date 过滤, 4 张大表无 date 索引导致全表 SCAN。
实测基线 (2026-08-18, 生产库, 建索引前):
- daily_klines (1273万行) WHERE date=? → SCAN, 27086ms
- stock_valuation WHERE date=? → SCAN, 480ms
- lhb_daily WHERE date=? → SCAN, 10ms
- stock_limit_up_down WHERE date=? → SCAN, 18ms
factor_daily / model_predictions 已有 PK autoindex (date 在 PK 首位), 无需建。

注意: CREATE INDEX 期间锁表, 大表 (daily_klines) 建索引耗时, 须在非交易时段
执行 (写库三步曲 + 用户 GO)。本模块只提供 DDL 与幂等建索引函数, 不自动执行。
"""

from __future__ import annotations

from common.logging import get_logger

logger = get_logger("data.indexes")

# 需建 date 二级索引的表 (EXPLAIN 验证为 SCAN 的 4 张)
DATE_INDEX_TABLES = [
    "daily_klines",
    "stock_valuation",
    "lhb_daily",
    "stock_limit_up_down",
]

# 索引 DDL — IF NOT EXISTS 幂等
INDEX_DDL = "\n".join(
    f"CREATE INDEX IF NOT EXISTS idx_{t}_date ON {t}(date);"
    for t in DATE_INDEX_TABLES
)


def ensure_indexes(db) -> dict[str, bool]:
    """对 4 张表建 date 二级索引 (幂等)。返回 {table: created_or_exists}。

    大表建索引耗时且锁表, 调用方须在非交易时段执行 (DL-16 风险条款)。
    """
    conn = db.get_connection()
    result: dict[str, bool] = {}
    for table in DATE_INDEX_TABLES:
        idx = f"idx_{table}_date"
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {idx} ON {table}(date)")
            conn.commit()
            result[table] = True
            logger.info(f"index ensured: {idx}")
        except Exception as e:
            result[table] = False
            logger.warning(f"index failed for {table}: {e}")
    return result


def list_date_indexes(db) -> list[str]:
    """返回当前库中已存在的 date 二级索引名 (供验收/测试)。"""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_%_date' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]
