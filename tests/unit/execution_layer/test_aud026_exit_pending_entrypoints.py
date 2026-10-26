"""AUD-026 回归: exit_pending 的入口必须统一走 mark_exit_trigger。

生产实证 2026-09-14: 22 只卡死 exit_pending 的 trigger_date/trigger_price **全 NULL**,
取证入池路径 = sell_signal(22 次) -> status_change(52 次), 没走 mark_exit_trigger。
后果: P0 AUD-005 的解冻方案 A (按各自触发日的 T+1 真收盘价回溯结算) **没有依据可用** ——
只能拿 updated_at 当代理。不修则每批新冻的仓位照样无元数据, 下次再冻还是查不出触发日。

实测三入口 (统筹在册记作"双入口", 实为三处):
  stock_pool.mark_exit_trigger      = 正门 (写 trigger_price/trigger_date/exit_reason/exit_low)
  exit_engine.py:94                 = 绕 (update_status 只写 status+updated_at)
  plan_manager.py:138               = 绕 (同上) —— 生产那 22 只走的就是这条
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


def _sell(el, code, reason="评分滑出"):
    el.plan_manager.receive_plan({
        "date": "2026-08-30",
        "recommendations": [{
            "code": code, "name": "t", "action": "sell",
            "confidence": 0.9, "reason": reason, "initial_score": 50,
        }],
    })


class TestSellSignalEntryPoint:
    """plan_manager sell 分支 —— 生产 22 只走的就是这条。"""

    def test_sell_signal_populates_trigger_metadata(self, el):
        el.stock_pool.add("000001", "t")
        el.stock_pool.update_status("000001", "active")
        _sell(el, "000001", reason="评分滑出")

        row = el.stock_pool.get("000001")
        assert row["status"] == "exit_pending"
        assert row["trigger_date"], (
            "trigger_date 必须落值 —— 全 NULL 正是 P0 解冻方案没依据可用的原因")
        assert row["exit_reason"], "exit_reason 必须落值 (结算时才不用兜底成 T+1确认卖出)"


class TestExitEngineEntryPoint:
    """exit_engine 六层判定 —— 另一条绕过正门的路。"""

    def test_layer_trigger_populates_trigger_metadata(self, el):
        el.stock_pool.add("000002", "t")
        el.stock_pool.update_status("000002", "active")
        # 建仓日不能当日卖 (T+1 闸), 故 entry_date 给昨天以前
        el.stock_pool.update("000002", entry_price=20.0, entry_date="2026-08-01",
                             position_qty=100, stop_loss=10.0)

        decision = el.exit_engine.check_exits("000002", current_price=9.0)

        assert decision is not None, "价格击穿硬止损须出决策"
        row = el.stock_pool.get("000002")
        assert row["status"] == "exit_pending"
        assert row["trigger_date"], "trigger_date 必须落值"
        assert row["trigger_price"] == 9.0, "trigger_price 须记触发时的真实价"


class TestPhantomSellGuardPreserved:
    """负向对照: 不得破 phantom 卖出修复 (非 active 不准被卖)。"""

    def test_pending_entry_still_not_sold(self, el):
        el.stock_pool.add("603345", "t")
        el.stock_pool.update_status("603345", "pending_entry")
        _sell(el, "603345")
        assert el.stock_pool.get("603345")["status"] == "watch"

    def test_watch_still_not_sold(self, el):
        el.stock_pool.add("000003", "t")
        el.stock_pool.update_status("000003", "watch")
        _sell(el, "000003")
        assert el.stock_pool.get("000003")["status"] != "exit_pending"
