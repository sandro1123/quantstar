"""S0 回归测试 — execution 层止血修复 (2026-08-16)

覆盖审计 P0: save_daily 查询 post_exit_tracking 用了 review_quality 列名,
而生产表实际列名是 quality → OperationalError 阻断整条 daily_update 链。
回归测试用 EXIT_TRACKING_DDL 的真实 schema 构造表, 确保列名漂移再次发生时测试失败。
"""
import pytest

from common.database import Database
from execution_layer.stats.performance import ExecutionStats
from execution_layer.tables import (
    ACTION_RECORDS_DDL,
    EXECUTION_STATS_DDL,
    EXIT_TRACKING_DDL,
    STOCK_POOL_DDL,
)


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(STOCK_POOL_DDL)
    conn.executescript(ACTION_RECORDS_DDL)
    conn.executescript(EXIT_TRACKING_DDL)
    conn.executescript(EXECUTION_STATS_DDL)
    conn.commit()
    yield database
    database.close()


def _seed(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, total_return_pct, exit_reason, exit_date) "
        "VALUES ('600001', 'A', 'exited', '2026-08-10', 0.05, '止盈', '2026-08-16'), "
        "('600002', 'B', 'exited', '2026-08-10', -0.03, '止损', '2026-08-16')"
    )
    conn.execute(
        "INSERT INTO action_records (code, action_type, reason, source, created_at) "
        "VALUES ('600001', 'entry', 'd', 'execution', '2026-08-16 10:00:00')"
    )
    conn.execute(
        "INSERT INTO post_exit_tracking "
        "(code, exit_date, exit_price, entry_price, total_return_pct, exit_reason, check_point, quality, created_at) "
        "VALUES ('600001', '2026-08-15', 10.0, 9.0, 0.11, '止盈', 'T+5', 'correct', '2026-08-16 10:00:00')"
    )
    conn.commit()


def test_save_daily_with_real_schema(db):
    """回归: save_daily 必须与生产 post_exit_tracking.quality 列名匹配"""
    _seed(db)
    stats = ExecutionStats(db)
    stats.save_daily("2026-08-16")
    row = db.get_connection().execute(
        "SELECT total_trades, win_count, correct_exits, exit_accuracy FROM execution_stats"
    ).fetchone()
    assert row["total_trades"] == 2
    assert row["win_count"] == 1
    assert row["correct_exits"] == 1
    assert row["exit_accuracy"] == 1.0


def test_save_daily_empty_tables(db):
    stats = ExecutionStats(db)
    stats.save_daily("2026-08-16")
    row = db.get_connection().execute("SELECT total_trades FROM execution_stats").fetchone()
    assert row["total_trades"] == 0


def _seed_multi_row_groups(db):
    """AUD-020 判例夹具: 每个分组 >= 2 行。

    一组一行的夹具让「数分组」与「数样本」两种实现恒等 —— 是分组计数病的
    天然盲区 (原 _seed 即如此)。本夹具每组多行, 使两者分叉。
    """
    conn = db.get_connection()
    # 3 只同因 '止损' 退出 + 2 只同因 '止盈' 退出 (各自 1 个分组, 多个成员)
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, total_return_pct, exit_reason, exit_date) "
        "VALUES ('600011','A','exited','2026-08-10',-0.03,'止损','2026-08-16'),"
        "       ('600012','B','exited','2026-08-10',-0.04,'止损','2026-08-16'),"
        "       ('600013','C','exited','2026-08-10',-0.05,'止损','2026-08-16'),"
        "       ('600014','D','exited','2026-08-10', 0.06,'止盈','2026-08-16'),"
        "       ('600015','E','exited','2026-08-10', 0.07,'止盈','2026-08-16')"
    )
    # 退出质量: correct x3, wrong x2, neutral x1 (共 6 行, 3 个分组)
    rows = [('600011','correct'),('600012','correct'),('600013','correct'),
            ('600014','wrong'),('600015','wrong'),('600016','neutral')]
    for code, q in rows:
        conn.execute(
            "INSERT INTO post_exit_tracking "
            "(code, exit_date, exit_price, entry_price, total_return_pct, exit_reason, "
            " check_point, quality, created_at) "
            "VALUES (?, '2026-08-15', 10.0, 9.0, 0.11, 'x', 'T+5', ?, '2026-08-16 10:00:00')",
            (code, q))
    conn.commit()


def test_save_daily_counts_samples_not_groups(db):
    """AUD-004 回归: GROUP BY 结果必须按 cnt 求和, 不是数分组行数。

    病版 sum(1 for r in rows ...) 数的是分组数 -> correct=1/wrong=1/neutral=1,
    exit_accuracy=1/3; 正版按样本数 -> 3/2/1, exit_accuracy=0.5。
    止损/止盈同理: 病版各 1, 正版 3 和 2。
    """
    _seed_multi_row_groups(db)
    stats = ExecutionStats(db)
    stats.save_daily("2026-08-16")
    row = db.get_connection().execute(
        "SELECT correct_exits, neutral_exits, wrong_exits, exit_accuracy, "
        "stop_loss_count, take_profit_count FROM execution_stats"
    ).fetchone()
    assert row["correct_exits"] == 3
    assert row["wrong_exits"] == 2
    assert row["neutral_exits"] == 1
    assert row["exit_accuracy"] == 0.5          # 3 / (3+2+1)
    assert row["stop_loss_count"] == 3
    assert row["take_profit_count"] == 2
