"""F4.6 人工审核消费路径测试 — approve → consume_approved → pending_entry"""
import pytest
from common.database import Database
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.human_in_loop import HumanInLoop
from execution_layer.core.plan_manager import PlanManager
from execution_layer.core.stock_pool import StockPool



@pytest.fixture(autouse=True)
def _pin_auto_approve_off(monkeypatch):
    """返工卡(同族): 锁死 hitl_auto_approve=False, 隔离生产 local.yaml 污染。

    与 test_ex01_approval_loop.py 同根因 — config/local.yaml (未跟踪生产
    override) hitl_auto_approve:true 使 confidence∈[0.5,0.7) 的 pending 审核
    测试走自动批准路径。此 2 红为预存 (pre-merge 0cf6348 即红), 非本次合并
    引入。confidence>=0.7 的 auto_approve 走 evaluate() 阈值分支, 不经
    _auto_approve_enabled(), 故锁 False 不影响 test_auto_approve_bypasses_queue。
    """
    monkeypatch.setattr(PlanManager, "_auto_approve_enabled", lambda self: False)

@pytest.fixture
def env():
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    ar = ActionRecord(db)
    ar._ensure_table()
    pm = PlanManager(db, pool, ar)
    return db, pool, pm


class TestApproveFix:
    def test_approve_nonexistent_returns_false(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        # total_changes bug 修复: 无匹配行必须返回 False
        assert h.approve(999) is False

    def test_approve_twice_returns_false_second_time(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "r", 0.5, 0.6)
        rid = h.get_pending()[0]["id"]
        assert h.approve(rid) is True
        assert h.approve(rid) is False  # 已非 pending

    def test_reject_nonexistent_returns_false(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        assert h.reject(999) is False

    def test_evaluate_stores_entry_params(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "r", 0.5, 0.6,
                   entry_low=10.0, entry_high=11.0, stop_loss=9.0, take_profit=13.0)
        rec = h.get_pending()[0]
        assert rec["entry_low"] == 10.0
        assert rec["entry_high"] == 11.0
        assert rec["stop_loss"] == 9.0
        assert rec["take_profit"] == 13.0


class TestConsumeApproved:
    def test_buy_approved_to_pending_entry(self, env):
        db, pool, pm = env
        h = pm.human_in_loop
        h.evaluate("000001", "buy", "低估值买入", 0.8, 0.6,
                   entry_low=10.0, entry_high=11.0, stop_loss=9.0, take_profit=13.0)
        rid = h.get_pending()[0]["id"]
        assert h.approve(rid) is True
        n = pm.consume_approved()
        assert n == 1
        row = pool.get("000001")
        assert row["status"] == "pending_entry"
        assert row["entry_low"] == 10.0
        assert row["entry_high"] == 11.0
        assert row["stop_loss"] == 9.0
        assert row["take_profit"] == 13.0

    def test_consume_idempotent(self, env):
        db, pool, pm = env
        h = pm.human_in_loop
        h.evaluate("000001", "buy", "r", 0.8, 0.6, entry_low=10.0, entry_high=11.0)
        h.approve(h.get_pending()[0]["id"])
        assert pm.consume_approved() == 1
        assert pm.consume_approved() == 0  # consumed 标记防重复

    def test_buy_skips_active_position(self, env):
        db, pool, pm = env
        pool.add("000001", "测试股")
        pool.update_status("000001", "active")
        h = pm.human_in_loop
        h.evaluate("000001", "buy", "重复买入", 0.8, 0.6, entry_low=10.0, entry_high=11.0)
        h.approve(h.get_pending()[0]["id"])
        assert pm.consume_approved() == 1  # 标记消费但跳过入场
        assert pool.get("000001")["status"] == "active"  # 未被改成 pending_entry

    def test_sell_approved_marks_exit_trigger(self, env):
        db, pool, pm = env
        pool.add("000001", "测试股")
        pool.update_status("000001", "active")
        pool.update("000001", last_price=12.0)
        h = pm.human_in_loop
        h.evaluate("000001", "sell", "止盈卖出", 0.8, 0.6)
        h.approve(h.get_pending()[0]["id"])
        assert pm.consume_approved() == 1
        row = pool.get("000001")
        assert row["status"] == "exit_pending"  # F4.4: 只标记, T+1 结算
        assert row["trigger_price"] == 12.0

    def test_sell_skips_non_active(self, env):
        db, pool, pm = env
        h = pm.human_in_loop
        h.evaluate("000001", "sell", "无持仓卖出", 0.8, 0.6)
        h.approve(h.get_pending()[0]["id"])
        assert pm.consume_approved() == 1
        assert pool.get("000001") is None

    def test_pending_not_consumed(self, env):
        db, pool, pm = env
        pm.human_in_loop.evaluate("000001", "buy", "未批准", 0.8, 0.6)
        assert pm.consume_approved() == 0
        assert pool.get("000001") is None


class TestReceivePlanReviewPath:
    def test_require_review_no_crash_and_queued(self, env):
        """回归: 审核路径 action_type 必须在白名单内 (原 buy_pending_review 会 IntegrityError)"""
        db, pool, pm = env
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.6, "reason": "中等置信", "initial_score": 70,
             "entry_low": 10.0, "entry_high": 11.0},
        ]})
        # 未直接入池, 进审核队列
        assert pool.get("000001") is None
        pending = pm.human_in_loop.get_pending()
        assert len(pending) == 1
        assert pending[0]["entry_low"] == 10.0
        # action_records 记了 skip_entry (白名单内)
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='skip_entry'"
        ).fetchall()
        assert len(rows) == 1

    def test_review_then_approve_then_consume_e2e(self, env):
        """端到端: 计划→审核→批准→消费→pending_entry"""
        db, pool, pm = env
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.6, "reason": "中等置信", "initial_score": 70,
             "entry_low": 10.0, "entry_high": 11.0, "stop_loss": 9.0, "take_profit": 13.0},
        ]})
        rid = pm.human_in_loop.get_pending()[0]["id"]
        pm.human_in_loop.approve(rid, reviewer="sandro", note="确认买入")
        assert pm.consume_approved() == 1
        row = pool.get("000001")
        assert row["status"] == "pending_entry"
        assert row["entry_high"] == 11.0

    def test_auto_approve_bypasses_queue(self, env):
        db, pool, pm = env
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.9, "reason": "高置信", "initial_score": 90,
             "entry_low": 10.0, "entry_high": 11.0,
             # REC 模块2 (2026-09-21): 守卫要求四参数齐 — 夹具补生产语义
             # (AUD-030 判例: 夹具比生产窄=病根镜像)
             "stop_loss": 9.5, "take_profit": 12.0},
        ]})
        assert pm.human_in_loop.get_pending() == []
        assert pool.get("000001")["status"] == "pending_entry"
