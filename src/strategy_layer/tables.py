"""Strategy Layer 表定义 — daily_regime 等"""

DAILY_REGIME_DDL = """
CREATE TABLE IF NOT EXISTS daily_regime (
    date TEXT PRIMARY KEY,
    regime TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    avg_change REAL,
    breadth REAL,
    volume_score REAL,
    composite_score REAL,
    detail TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

STRATEGY_TABLES = [
    DAILY_REGIME_DDL,
    "CREATE INDEX IF NOT EXISTS idx_daily_regime_date ON daily_regime(date);",
]


def init_strategy_tables(conn):
    for ddl in STRATEGY_TABLES:
        conn.execute(ddl)
    conn.commit()
