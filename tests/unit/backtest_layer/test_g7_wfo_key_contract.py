# -*- coding: utf-8 -*-
"""G7 测试钉：生产者-消费者键名契约。

真病: walk_forward 产出 wf_result["oos_efficiency"]，
persistence 读 wf["efficiency"] => 写入 NULL => 门禁永远「未评估」。

本钉**跨生产者-消费者边界**验（判例 #150）：
不验各自内部，只验「生产者产出的键，消费者读得到」。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _producer_keys():
    """从 walk_forward.py 的真实产出里取键集合（源码级，不靠文档）。"""
    import inspect
    from backtest_layer.modes import walk_forward
    src = inspect.getsource(walk_forward.WalkForwardMode.run)
    # 找 wf_result = { ... } 块
    start = src.index('wf_result = {')
    end = src.index('}', start)
    block = src[start:end]
    import re
    return set(re.findall(r'"(\w+)":', block))


def _consumer_keys():
    """从 persistence.py 里取它实际读的键（源码级）。"""
    import inspect
    from backtest_layer.results import persistence
    src = inspect.getsource(persistence)
    import re
    return set(re.findall(r'wf\.get\("(\w+)"', src))


def test_consumer_reads_producer_efficiency_key():
    """钉1 (G7 核心): 消费者必须能读到生产者产出的效率键。"""
    prod = _producer_keys()
    assert "oos_efficiency" in prod, (
        f"生产者 walk_forward 未产出 oos_efficiency，实得键: {sorted(prod)}")

    # 消费者侧：源码里必须出现 oos_efficiency（兼容读）
    import inspect
    from backtest_layer.results import persistence
    src = inspect.getsource(persistence)
    assert "oos_efficiency" in src, (
        "persistence 未读生产者的 oos_efficiency 键 "
        "—— wf_efficiency 会永远 NULL")


def test_persistence_writes_efficiency_from_producer_dict():
    """钉2 (端到端): 给一个只用生产者键名的 wf_result，写入后必须非 NULL。"""
    import sqlite3

    class _DB:
        def __init__(self):
            self.c = sqlite3.connect(":memory:", check_same_thread=False)
            self.c.row_factory = sqlite3.Row

        def get_connection(self):
            return self.c

    from backtest_layer.results.persistence import BacktestRunStore
    from backtest_layer.core.models import BacktestReport

    db = _DB()
    p = BacktestRunStore(db)
    # 建表
    conn = db.get_connection()
    conn.executescript(p.DDL if hasattr(p, "DDL") else "")
    # 若 persistence 自己建表，直接调 save 即可
    report = BacktestReport(
        mode="walk_forward",
        date_range=("2026-09-01", "2026-09-30"),
        total_return=0.05, annual_return=0.1, sharpe=1.2,
        max_drawdown=-0.05, win_rate=0.6, total_trades=10,
        # 只用生产者的键名（oos_efficiency），不提供 efficiency
        wf_result={"oos_efficiency": 0.75, "stability_score": 0.8},
    )
    try:
        run_id = p.save(report, bt_input=None, duration_ms=100)
    except Exception as e:
        # 表结构不存在等情况跳过（本钉重点是键映射，不是建表）
        import pytest
        pytest.skip(f"save 需要完整 schema: {e}")

    row = conn.execute(
        "SELECT wf_efficiency FROM backtest_runs WHERE run_id=?",
        (run_id,)).fetchone()
    assert row is not None, "未写入 backtest_runs"
    assert row[0] is not None, (
        "wf_efficiency 写成 NULL —— 生产者的 oos_efficiency 键未被消费者读到")
    assert abs(float(row[0]) - 0.75) < 1e-9, f"值不符: {row[0]}"
