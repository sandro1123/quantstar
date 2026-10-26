"""F4.1 & EX-10 stock_pool schema 迁移测试 — 旧 schema → 迁移 → 列齐/默认值/幂等"""
import pytest
from common.database import Database
from execution_layer.migrations import MIGRATIONS, TARGET_VERSION, ensure_schema

# 生产库迁移前的旧 schema (53 列, 缺 12 列 + exit_source) — 精简到迁移测试关心的核心列
OLD_SCHEMA_DDL = """
CREATE TABLE stock_pool (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    added_date TEXT NOT NULL,
    entry_price REAL,
    entry_date TEXT,
    position_qty INTEGER DEFAULT 0,
    stop_loss REAL,
    trailing_stop REAL,
    take_profit REAL,
    exit_price REAL,
    exit_date TEXT,
    total_return_pct REAL,
    holding_days INTEGER DEFAULT 0,
    last_price REAL,
    review_note TEXT,
    review_quality TEXT,
    updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

EXPECTED_V2_COLUMNS = {
    "atr_stop", "total_fees", "net_return", "net_return_pct",
    "add_position_count", "reduce_position_count", "partial_exit_count",
    "swap_count", "latest_adjustment_action", "latest_adjustment_date",
    "position_pct_initial", "avg_entry_price",
}

EXPECTED_ALL_NEW_COLUMNS = EXPECTED_V2_COLUMNS | {"exit_source"}


@pytest.fixture
def old_db():
    """:memory: 库构造旧 schema 版本 (schema_version=1, stock_pool 缺 12 列 + exit_source)"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute(OLD_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, entry_price, position_qty) "
        "VALUES ('000001', '平安银行', 'active', '2026-08-01', 10.0, 100)"
    )
    conn.commit()
    yield db
    db.close()


def _columns(db) -> set[str]:
    return {r[1] for r in db.get_connection().execute("PRAGMA table_info(stock_pool)")}


def _migration_statement_count() -> int:
    return sum(len(statements) for _, statements in MIGRATIONS)


def _alert_alter_count() -> int:
    """v8 的 alerts ALTER 条数。old_db 夹具无 alerts 表，ensure_schema 按目标表
    PRAGMA 判定会正确跳过它们 — 计数契约须排除，否则拿总语句数当应应用数。"""
    return sum(
        1 for _, statements in MIGRATIONS for stmt in statements
        if stmt.startswith("ALTER TABLE alerts")
    )


def test_migrations_declares_all_versions():
    """声明式清单覆盖 v2-v8，目标版本始终跟随最高迁移版本。"""
    by_version = dict(MIGRATIONS)
    assert TARGET_VERSION == max(by_version)
    assert set(by_version) >= {2, 3, 4, 5, 6, 7, 8}
    declared_v2 = set()
    for stmt in by_version[2]:
        assert stmt.startswith("ALTER TABLE stock_pool ADD COLUMN")
        declared_v2.add(stmt.split("ADD COLUMN")[1].strip().split()[0])
    assert declared_v2 == EXPECTED_V2_COLUMNS
    # v3: delivery_log 建表 + 索引
    assert any("delivery_log" in s for s in by_version[3])
    # v4: exit_source
    assert any("exit_source" in s for s in by_version[4])
    # v5: memory_store 建表 + 2 索引
    assert any("memory_store" in s for s in by_version[5])
    # v6: chart_analysis 建表 + 2 索引
    assert any("chart_analysis" in s for s in by_version[6])
    # v7: report_section_trace 建表 + 索引
    assert any("report_section_trace" in s for s in by_version[7])
    # v8: alerts 持续观测两列 (告警卡, 2026-09-17)
    v8 = by_version[8]
    assert any("last_escalated_at" in s for s in v8)
    assert any("last_seen" in s for s in v8)
    assert all(s.startswith("ALTER TABLE alerts") for s in v8)


def test_migrate_adds_missing_columns(old_db):
    """旧 schema → ensure_schema → 所有新列全部存在 + delivery_log 建表"""
    result = ensure_schema(old_db)
    assert result["version_before"] == 1
    assert result["version_after"] == TARGET_VERSION
    # 夹具无 alerts 表 → v8 两条 alerts ALTER 被正确跳过, 不计入 applied
    assert len(result["applied"]) == _migration_statement_count() - _alert_alter_count()
    cols = _columns(old_db)
    assert EXPECTED_ALL_NEW_COLUMNS <= cols


def test_migrate_defaults(old_db):
    """新增列默认值: 计数列=0, total_fees=0, 其余 NULL; 存量行不受影响"""
    ensure_schema(old_db)
    row = old_db.get_connection().execute(
        "SELECT total_fees, add_position_count, reduce_position_count, "
        "partial_exit_count, swap_count, atr_stop, net_return, avg_entry_price, "
        "exit_source, entry_price, position_qty FROM stock_pool WHERE code='000001'"
    ).fetchone()
    assert row["total_fees"] == 0
    assert row["add_position_count"] == 0
    assert row["reduce_position_count"] == 0
    assert row["partial_exit_count"] == 0
    assert row["swap_count"] == 0
    assert row["atr_stop"] is None
    assert row["net_return"] is None
    assert row["avg_entry_price"] is None
    assert row["exit_source"] is None
    # 存量数据完好
    assert row["entry_price"] == 10.0
    assert row["position_qty"] == 100


def test_migrate_idempotent(old_db):
    """二次运行: 零语句, 版本不变, 无重复版本行"""
    first = ensure_schema(old_db)
    assert len(first["applied"]) == _migration_statement_count() - _alert_alter_count()
    second = ensure_schema(old_db)
    assert second["applied"] == []
    assert second["version_before"] == TARGET_VERSION
    assert second["version_after"] == TARGET_VERSION
    versions = old_db.get_connection().execute(
        "SELECT version FROM schema_version ORDER BY version"
    ).fetchall()
    assert [r[0] for r in versions] == [1, TARGET_VERSION]


def test_migrate_partial_columns_present(old_db):
    """部分列已存在 (手工补过) → 只补缺失列, 不报 duplicate column"""
    old_db.get_connection().execute("ALTER TABLE stock_pool ADD COLUMN atr_stop REAL")
    old_db.get_connection().commit()
    result = ensure_schema(old_db)
    assert len(result["applied"]) == _migration_statement_count() - _alert_alter_count() - 1
    assert EXPECTED_ALL_NEW_COLUMNS <= _columns(old_db)


def test_migrate_fresh_db_no_stock_pool():
    """全新库 (无 stock_pool) → 跳过 ALTER 列补齐, 但 delivery_log 仍建表"""
    from execution_layer import ExecutionLayer
    db = Database(":memory:")
    db.init_db()
    result = ensure_schema(db)
    # 全新库既无 stock_pool 也无 alerts → 两族 ALTER 都被按目标表判定正确跳过
    non_alter_count = sum(
        1 for _, statements in MIGRATIONS for stmt in statements
        if not stmt.startswith("ALTER TABLE stock_pool")
        and not stmt.startswith("ALTER TABLE alerts")
    )
    assert len(result["applied"]) == non_alter_count
    # 表名清单从 MIGRATIONS 动态推导（判例：版本号断言禁写死同族——
    # 迁移清单加新表时此断言不应再被写死元组绊倒）
    import re as _re
    table_names = tuple({
        m.group(1)
        for _, statements in MIGRATIONS for stmt in statements
        if not stmt.startswith("ALTER TABLE")
        for m in [_re.search(r"(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", stmt)]
        if m
    })
    assert all(any(name in stmt for name in table_names) for stmt in result["applied"])
    assert result["version_after"] == TARGET_VERSION
    # start() 用完整 DDL 建表 → 全齐
    el = ExecutionLayer(db=db)
    el.start()
    assert EXPECTED_ALL_NEW_COLUMNS <= _columns(db)
    el.shutdown()
    db.close()


def test_migrate_crud_on_new_columns(old_db):
    """迁移后新列可读写 (CRUD 验证)"""
    ensure_schema(old_db)
    conn = old_db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET atr_stop=?, total_fees=?, add_position_count=?, "
        "avg_entry_price=?, latest_adjustment_action=?, latest_adjustment_date=?, exit_source=? "
        "WHERE code='000001'",
        (2.5, 12.3, 1, 10.05, "add_position", "2026-08-17", "engine"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM stock_pool WHERE code='000001'").fetchone()
    assert row["atr_stop"] == 2.5
    assert row["total_fees"] == 12.3
    assert row["add_position_count"] == 1
    assert row["avg_entry_price"] == 10.05
    assert row["latest_adjustment_action"] == "add_position"
    assert row["latest_adjustment_date"] == "2026-08-17"
    assert row["exit_source"] == "engine"
