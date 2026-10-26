"""adjust_tables.py — DL-19 复权元数据表 (D96: dividends/splits/adjust_factors)

F-20 / 对账表 D96: 复权元数据表不存在。复权因子/分红/拆股事件无处落地,
回测复权还原与估值精修缺少数据基础。

数据源缺口 (2026-08-18 实测): fuyao_client 目前仅提供 financials / prices /
special-data / auction 端点, 无复权因子/分红/拆股事件流端点 (候选路径全 404)。
故本卡只建表 + 提供幂等建表函数, 并标注数据源缺口, 不硬闯接采集管线。
待未来出现可用复权事件流, 再补管线写入本组表。

本模块只提供 DDL 与幂等建表函数, 不自动执行。
生产建表需用户 GO (写库三步曲)。
"""

from __future__ import annotations

from common.logging import get_logger

logger = get_logger("data.adjust_tables")

# 复权元数据三表 DDL — IF NOT EXISTS 幂等
# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
ADJUST_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS dividends (
    code TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    announce_date TEXT DEFAULT '',
    record_date TEXT DEFAULT '',
    pay_date TEXT DEFAULT '',
    cash_dividend REAL DEFAULT 0,
    stock_dividend REAL DEFAULT 0,
    stock_transfer REAL DEFAULT 0,
    source TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, ex_date)
);
CREATE INDEX IF NOT EXISTS idx_dividends_ex_date ON dividends(ex_date);

CREATE TABLE IF NOT EXISTS splits (
    code TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    split_factor REAL DEFAULT 1.0,
    source TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, ex_date)
);
CREATE INDEX IF NOT EXISTS idx_splits_ex_date ON splits(ex_date);

CREATE TABLE IF NOT EXISTS adjust_factors (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    adjust_factor REAL DEFAULT 1.0,
    source TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_adjust_factors_date ON adjust_factors(date);
"""

ADJUST_TABLES = ["dividends", "splits", "adjust_factors"]


def ensure_adjust_tables(db) -> dict[str, bool]:
    """幂等建 dividends/splits/adjust_factors 三表。返回 {table: 存在或已建}。

    只建表不写数据; 生产执行须用户 GO (写库三步曲)。
    """
    conn = db.get_connection()
    conn.executescript(ADJUST_TABLES_DDL)
    conn.commit()
    result: dict[str, bool] = {}
    for table in ADJUST_TABLES:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        result[table] = row is not None
        logger.info(f"adjust table ensured: {table}")
    return result
