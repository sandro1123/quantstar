"""P0 phantom 卖出修复 — RED 先行复现 (C 取证 12 只 phantom 实证)

根因: plan_manager.receive_plan sell 分支不校验 status,
pending_entry/watch 直接被打成 exit_pending → 从未建仓的股票被卖出结算。
修法 (对齐 holding.py:26 "mark_exit_trigger 只认 active" 同款语义):
  active       → exit_pending (原行为, 结算走 _settle_pending_exits T+1)
  pending_entry→ cancel_entry (入场计划作废, 对齐 holding.py 复评不过分支)
  watch        → 移除 watch (评分滑出本来就该出 watch)
"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture
def el():
    db = Database(":memory:")
    db.init_db()
    layer = ExecutionLayer(db=db)
    layer.start()
    yield layer
    layer.shutdown()
    db.close()


def _sell(el, code):
    el.plan_manager.receive_plan({
        "date": "2026-08-30",
        "recommendations": [{
            "code": code, "name": "t", "action": "sell",
            "confidence": 0.9, "reason": "评分滑出",
            "initial_score": 50,
        }],
    })


class TestPhantomSellRed:
    def test_pending_entry_not_sold(self, el):
        """RED: pending_entry 股票被 sell_signal 打成 exit_pending (C 案例 603345)"""
        el.stock_pool.add("603345", "t")
        el.stock_pool.update_status("603345", "pending_entry")
        _sell(el, "603345")
        assert el.stock_pool.get("603345")["status"] != "exit_pending"

    def test_watch_not_sold(self, el):
        """RED: watch 股票被 sell_signal 打成 exit_pending"""
        el.stock_pool.add("000002", "t")
        el.stock_pool.update_status("000002", "watch")
        _sell(el, "000002")
        assert el.stock_pool.get("000002")["status"] != "exit_pending"

    def test_active_still_exits(self, el):
        """GREEN 保障: active 的 sell 行为不变 (真持仓才准卖)"""
        el.stock_pool.add("000001", "t")
        el.stock_pool.update_status("000001", "active")
        _sell(el, "000001")
        assert el.stock_pool.get("000001")["status"] == "exit_pending"


class TestPhantomSellSemantics:
    """修复后语义: 非-active 状态的 sell 走 cancel_entry 留痕, 可审计"""

    def _actions(self, el, code):
        conn = el.db.get_connection()
        return [r[0] for r in conn.execute(
            "SELECT action_type FROM action_records WHERE code=? ORDER BY id",
            (code,)).fetchall()]

    def test_pending_entry_sell_writes_cancel_entry(self, el):
        el.stock_pool.add("603345", "t")
        el.stock_pool.update_status("603345", "pending_entry")
        _sell(el, "603345")
        pool = el.stock_pool.get("603345")
        assert pool["status"] == "watch"
        assert "cancel_entry" in self._actions(el, "603345")
        assert "sell_signal" not in self._actions(el, "603345")

    def test_watch_sell_moves_out_of_watch(self, el):
        el.stock_pool.add("000002", "t")
        el.stock_pool.update_status("000002", "watch")
        _sell(el, "000002")
        pool = el.stock_pool.get("000002")
        assert pool["status"] == "exited"
        assert "cancel_entry" in self._actions(el, "000002")

    def test_no_pool_row_sell_is_noop(self, el):
        """池里没有该股: sell 不建行不写台账 (原 if current 语义保留)"""
        before = el.db.get_connection().execute(
            "SELECT COUNT(*) FROM stock_pool").fetchone()[0]
        _sell(el, "999999")
        after = el.db.get_connection().execute(
            "SELECT COUNT(*) FROM stock_pool").fetchone()[0]
        assert before == after
