"""AUD-007/010 甲案回归: 调仓族内脏自洽 (统筹 sm-facfb9d9 裁甲案)。

背景: 调仓族五个面全部在册 (四写方法 + 四计数列 + avg_entry_price +
action_records 词表 + HITL 中文标签), 但零生产调用方。
**更要紧的是即使接上调用方也不会对**:
  - 四计数列没人维护 -> 通电后计数仍恒 0
  - avg_entry_price 没人写 -> 加仓后盈亏拿第一次入场价算 = 通电了也算错
  - 四者均 `if active: 做事` 无 else -> 静默 no-op, 调用方分不清"做了"与"被丢了"

甲案 = 先修内脏不接线, 让五面从"零面通电"变"内部自洽候接线";
将来接线只需加调用方, 不必回头补内脏。乙案(整件通电)属功能开发另立卡。
"""
import logging

import pytest

from common.database import Database
from execution_layer.core.stock_pool import StockPool


@pytest.fixture
def pool():
    db = Database(":memory:")
    p = StockPool(db)
    p._ensure_table()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, entry_price, "
        "position_qty, last_price) "
        "VALUES ('000001', 't', 'active', '2026-08-01', 10.0, 1000, 10.0)")
    conn.commit()
    yield p
    db.close()


class TestCounterColumnsMaintained:
    """四计数列: DDL 声明了就必须有人维护, 否则通电后恒 0。"""

    def test_add_position_increments_counter(self, pool):
        pool.add_position("000001", 500, 12.0, "加仓")
        assert pool.get("000001")["add_position_count"] == 1
        pool.add_position("000001", 300, 13.0, "再加")
        assert pool.get("000001")["add_position_count"] == 2

    def test_reduce_position_increments_counter(self, pool):
        pool.reduce_position("000001", 200, 11.0, "减仓")
        assert pool.get("000001")["reduce_position_count"] == 1

    def test_partial_exit_increments_counter(self, pool):
        pool.partial_exit("000001", 30.0, 11.0, "部分止盈")
        assert pool.get("000001")["partial_exit_count"] == 1


class TestAvgEntryPriceMaintained:
    """AUD-010: avg_entry_price 有 DDL+migration+白名单却零写者 —— 加仓后盈亏会用第一次入场价算。"""

    def test_add_position_updates_weighted_average(self, pool):
        # 原: 1000 股 @10.0; 加: 500 股 @13.0 -> 均价 = (10000+6500)/1500 = 11.0
        pool.add_position("000001", 500, 13.0, "加仓")
        row = pool.get("000001")
        assert row["position_qty"] == 1500
        assert row["avg_entry_price"] == pytest.approx(11.0), (
            "加仓后必须更新加权均价, 否则盈亏拿第一次入场价算 = 通电了也算错")

    def test_first_add_seeds_from_entry_price(self, pool):
        """首次加仓时 avg_entry_price 尚为 NULL, 须以 entry_price 为起点。"""
        pool.add_position("000001", 1000, 20.0, "加仓")
        assert pool.get("000001")["avg_entry_price"] == pytest.approx(15.0)  # (10+20)/2

    def test_reduce_does_not_change_average(self, pool):
        """减仓只减量不改均价 (会计常识: 卖出不影响持仓成本)。"""
        pool.add_position("000001", 500, 13.0, "加仓")
        before = pool.get("000001")["avg_entry_price"]
        pool.reduce_position("000001", 300, 12.0, "减仓")
        assert pool.get("000001")["avg_entry_price"] == pytest.approx(before)


class TestSilentNoOpMadeVisible:
    """静默跳过家族: 非 active 时四者静默返回 None, 调用方分不清做了还是被丢了。"""

    @pytest.fixture
    def watch_pool(self, pool):
        # D席 pool-position-guard (2026-09-16): 原用 update_status('watch') 造态,
        # 但守卫后该病态动作已被正门拒绝 (带仓票不得进候选态) —— 这正是守卫的
        # 目标行为。夹具意图是「非 active 态带仓行」(存量病态形态), 改直写造态,
        # 测的仍是 _require_active 对非 active 的显形, 语义不变。
        conn = pool.db.get_connection()
        conn.execute("UPDATE stock_pool SET status='watch' WHERE code='000001'")
        conn.commit()
        return pool

    def test_add_position_on_non_active_returns_false_and_warns(self, watch_pool, caplog):
        with caplog.at_level(logging.WARNING):
            ok = watch_pool.add_position("000001", 500, 12.0, "加仓")
        assert ok is False, "被丢弃必须能被调用方看见 (返回值), 不能只是 None"
        assert "000001" in caplog.text and "active" in caplog.text.lower()

    def test_success_returns_true(self, pool):
        assert pool.add_position("000001", 500, 12.0, "加仓") is True
        assert pool.reduce_position("000001", 100, 11.0, "减仓") is True
        assert pool.partial_exit("000001", 10.0, 11.0, "部分止盈") is True

    def test_reduce_and_partial_exit_also_visible(self, watch_pool, caplog):
        with caplog.at_level(logging.WARNING):
            assert watch_pool.reduce_position("000001", 100, 11.0, "减仓") is False
            assert watch_pool.partial_exit("000001", 10.0, 11.0, "部分止盈") is False
        assert caplog.text.count("000001") >= 2


class TestUncoveredBranches:
    """自查补测: 上面的用例恰好没走到 partial_exit 全退分支与 swap ——
    这两条路径用了 bj_date(), 而本模块无模块级 import (别处都是函数内局部 import)。
    覆盖盲区就是这么形成的: 测了三个计数器却没测到用不同符号的那条支。"""

    def test_partial_exit_full_closes_position(self, pool):
        ok = pool.partial_exit("000001", 100.0, 12.0, "全部止盈")
        assert ok is True
        row = pool.get("000001")
        assert row["status"] == "exited"
        assert row["position_qty"] == 0
        assert row["exit_price"] == 12.0
        assert len(row["exit_date"]) == 10, (
            "exit_date 须写日期形态 —— execution_stats.save_daily 用 exit_date=? "
            "精确等值比日期串, 时间戳形态永远匹配不上, 这些了结会静默不进当日统计")
        assert row["partial_exit_count"] == 1

    def test_swap_exits_old_and_counts(self, pool):
        ok = pool.swap("000001", "000002", "调仓")
        assert ok is True
        row = pool.get("000001")
        assert row["status"] == "exited"
        assert len(row["exit_date"]) == 10, "同上: 日期形态"
        assert row["swap_count"] == 1

    def test_swap_on_non_active_returns_false(self, pool, caplog):
        import logging
        # 同上: 守卫后 update_status('watch') 造不出非 active 带仓态, 改直写。
        conn = pool.db.get_connection()
        conn.execute("UPDATE stock_pool SET status='watch' WHERE code='000001'")
        conn.commit()
        with caplog.at_level(logging.WARNING):
            assert pool.swap("000001", "000002", "调仓") is False
        assert "swap" in caplog.text
