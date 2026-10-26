"""P1 M1 盯市落库 — RED 先行

缺陷一 (绝对值列零写入): pnl_tracker.daily_update 只写 unrealized_pnl_pct;
绝对值列 unrealized_pnl 全 repo 无写入者 (生产 2 active 仓 NULL 实证)。
修法: marks 同点补写 unrealized_pnl = (close - entry_price) * position_qty。

缺陷二 (快照计算源折 0): monitor._record_pnl_snapshot 计算源
`item.get("unrealized_pnl_pct", 0) or 0` 把从未盯市的 None 静默折 0 —
快照把"不知道"记成"零收益"。违「宁可 NULL 不可假值」宪法。
修法: 只对真实盯市过的仓位求和; 全部未盯市 → 快照落 NULL (unknown ≠ 0)。
"""
import sys
sys.path.insert(0, "src")

from common.database import Database
from execution_layer import ExecutionLayer

import pytest


class _Kline:
    def __init__(self, code, close):
        self.code = code
        self.close = close


@pytest.fixture
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _active_with_entry(el, code="000001", entry=10.0, qty=100):
    el.stock_pool.add(code, "测试")
    el.stock_pool.update_status(code, "active")
    el.stock_pool.update(code, entry_price=entry, position_qty=qty)


class TestPnLTrackerAbsolutePnl:
    def test_daily_update_writes_unrealized_pnl_absolute(self, el):
        """RED: marks 后 active 仓 unrealized_pnl 必须非 NULL (现况=从不写)"""
        _active_with_entry(el, entry=10.0, qty=100)
        el.pnl_tracker.daily_update([_Kline("000001", 11.0)])
        pool = el.stock_pool.get("000001")
        assert pool["unrealized_pnl"] is not None, \
            "marks 后 unrealized_pnl 仍为 NULL — 绝对值列无人写入"
        assert pool["unrealized_pnl"] == pytest.approx((11.0 - 10.0) * 100)

    def test_daily_update_multiple_stocks_absolute_pnl(self, el):
        _active_with_entry(el, "000001", entry=10.0, qty=100)
        _active_with_entry(el, "000002", entry=20.0, qty=200)
        el.pnl_tracker.daily_update([_Kline("000001", 12.0), _Kline("000002", 18.0)])
        p1 = el.stock_pool.get("000001")
        p2 = el.stock_pool.get("000002")
        assert p1["unrealized_pnl"] == pytest.approx((12.0 - 10.0) * 100)
        assert p2["unrealized_pnl"] == pytest.approx((18.0 - 20.0) * 200)

    def test_daily_update_loss_position_absolute_pnl_negative(self, el):
        _active_with_entry(el, entry=10.0, qty=100)
        el.pnl_tracker.daily_update([_Kline("000001", 9.0)])
        pool = el.stock_pool.get("000001")
        assert pool["unrealized_pnl"] == pytest.approx(-100.0)

    def test_daily_update_pct_still_written(self, el):
        """守卫: 修复不得破坏既有 pct 写入 (回归面)"""
        _active_with_entry(el, entry=10.0, qty=100)
        el.pnl_tracker.daily_update([_Kline("000001", 11.0)])
        pool = el.stock_pool.get("000001")
        assert pool["unrealized_pnl_pct"] == pytest.approx(10.0)

    def test_no_kline_leaves_columns_null(self, el):
        """无行情 → 不写不折 0 (既有 continue 语义保留)"""
        _active_with_entry(el, entry=10.0, qty=100)
        el.pnl_tracker.daily_update([])
        pool = el.stock_pool.get("000001")
        assert pool["unrealized_pnl"] is None
        assert pool["unrealized_pnl_pct"] is None


class TestSnapshotCalcSource:
    """快照计算源: None (从未盯市) 不得折 0 — unknown ≠ zero"""

    def _snapshots(self, el):
        return el.db.get_connection().execute(
            "SELECT unrealized_pnl_pct, active_count FROM intraday_pnl_snapshots"
        ).fetchall()

    def test_snapshot_all_never_marked_records_null(self, el):
        """RED: 全部仓位从未盯市 → 快照必须 NULL (不可算), 不得落 0.0 假平盘"""
        active = [{"code": "000001", "unrealized_pnl_pct": None}]
        el.intraday_monitor._record_pnl_snapshot(active)
        rows = self._snapshots(el)
        assert rows[-1]["unrealized_pnl_pct"] is None, \
            "从未盯市落 0.0 = 把'不知道'记成'零收益' (假值)"

    def test_snapshot_excludes_none_sums_marked_only(self, el):
        """None 剔除: 只对真实盯市过的仓位求和 (2.5), None 仓位不计入"""
        active = [
            {"code": "000001", "unrealized_pnl_pct": 2.5},   # 真实盯市
            {"code": "000002", "unrealized_pnl_pct": None},  # 从未盯市
        ]
        el.intraday_monitor._record_pnl_snapshot(active)
        rows = self._snapshots(el)
        assert rows[-1]["unrealized_pnl_pct"] == pytest.approx(2.5)
        assert rows[-1]["active_count"] == 2

    def test_snapshot_negative_pnl_sum(self, el):
        active = [{"code": "000001", "unrealized_pnl_pct": -3.0},
                  {"code": "000002", "unrealized_pnl_pct": 1.0}]
        el.intraday_monitor._record_pnl_snapshot(active)
        rows = self._snapshots(el)
        assert rows[-1]["unrealized_pnl_pct"] == pytest.approx(-2.0)

    def test_snapshot_explicit_zero_is_kept(self, el):
        """真 0 (盯过市且恰 0) 与 None (从未盯市) 语义不同: 真 0 照常计入"""
        active = [{"code": "000001", "unrealized_pnl_pct": 0.0}]
        el.intraday_monitor._record_pnl_snapshot(active)
        rows = self._snapshots(el)
        assert rows[-1]["unrealized_pnl_pct"] == pytest.approx(0.0)
        assert rows[-1]["active_count"] == 1

    def test_snapshot_empty_active_records_zero(self, el):
        """空仓 → 0.0 是真值 (没有仓位=没有盈亏), 与 None 语义不同"""
        el.intraday_monitor._record_pnl_snapshot([])
        rows = self._snapshots(el)
        assert rows[-1]["unrealized_pnl_pct"] == pytest.approx(0.0)
        assert rows[-1]["active_count"] == 0
