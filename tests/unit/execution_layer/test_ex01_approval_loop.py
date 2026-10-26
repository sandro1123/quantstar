"""EX-01+02: HITL 审批闭环 — approve/reject + CLI + 无参数单打回 + 存量 invalid + 飞书推送

先写失败测试: "批准无参数单 → 打回"。
验收: CLI 可批准/拒绝; 无参数单打回标记 invalid 不消费; 存量无参数 approved 标记 invalid 不删除;
pending 审核经 delivery 推送飞书卡片。
"""
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from common.database import Database
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.human_in_loop import HumanInLoop
from execution_layer.core.plan_manager import PlanManager
from execution_layer.core.stock_pool import StockPool



@pytest.fixture(autouse=True)
def _pin_auto_approve_off(monkeypatch):
    """返工卡: 锁死 hitl_auto_approve=False, 隔离生产 local.yaml 污染。

    根因: config/local.yaml (未跟踪生产 override) 有 hitl_auto_approve:true,
    cfg_get 读之 → _auto_approve_enabled()=True → confidence∈[0.5,0.7) 的
    pending 审核测试走自动批准路径 → get_pending()==0 断言失败。
    本文件无一条测试需要 auto_approve=ON (该路径在 test_hitl_auto.py),
    故全文件锁 False, 对齐 test_hitl_auto.py:157 的 monkeypatch 手法。
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


class TestApproveNoParamsRejected:
    """先写失败测试: 批准无参数单 → 打回"""

    def test_approve_no_params_buy_rejected(self, env):
        """批准无入场参数的 buy → consume 打回标记 invalid, 不入池, 记 action_record"""
        db, pool, pm = env
        h = pm.human_in_loop
        # 无参数批准单 (entry_low/high/stop_loss/take_profit 全 0)
        h.evaluate("000001", "buy", "无参数推荐", 0.8, 0.6)
        rid = h.get_pending()[0]["id"]
        assert h.approve(rid) is True
        n = pm.consume_approved()
        assert n == 1  # 处理了 (打回也算处理)
        # 不入池
        assert pool.get("000001") is None
        # 标记 invalid 不删除
        rec = h.get(rid)
        assert rec["status"] == "invalid"
        assert "缺少入场参数" in (rec.get("review_note") or "")
        # action_record 记 skip_entry + 明确 reason
        rows = db.get_connection().execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='skip_entry'"
        ).fetchall()
        assert len(rows) == 1
        assert "缺入场参数" in rows[0]["reason"]

    def test_existing_paramless_approved_marked_invalid_not_deleted(self, env):
        """存量无参数 approved 记录: 标记 invalid 不消费不删除"""
        db, pool, pm = env
        h = pm.human_in_loop
        # 模拟存量: 直接插入 approved 无参数记录 (如历史 7 条)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO human_review_queue (code, decision_type, reason, score, confidence, "
            "entry_low, entry_high, stop_loss, take_profit, status, consumed) "
            "VALUES ('000002', 'buy', '存量无参数', 0.8, 0.6, 0, 0, 0, 0, 'approved', 0)")
        conn.commit()
        rid = conn.execute("SELECT id FROM human_review_queue WHERE code='000002'").fetchone()[0]
        n = pm.consume_approved()
        assert n == 1
        rec = h.get(rid)
        assert rec is not None, "不删除, 留痕"
        assert rec["status"] == "invalid"
        assert pool.get("000002") is None

    def test_approve_with_params_still_consumes(self, env):
        """回归: 有参数的批准单正常消费到 pending_entry"""
        db, pool, pm = env
        h = pm.human_in_loop
        h.evaluate("000003", "buy", "有参数", 0.8, 0.6,
                   entry_low=10.0, entry_high=11.0, stop_loss=9.0, take_profit=13.0)
        rid = h.get_pending()[0]["id"]
        h.approve(rid)
        assert pm.consume_approved() == 1
        row = pool.get("000003")
        assert row["status"] == "pending_entry"
        assert row["entry_low"] == 10.0


class TestMarkInvalid:
    def test_mark_invalid_pending(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "r", 0.5, 0.6)
        rid = h.get_pending()[0]["id"]
        assert h.mark_invalid(rid, "测试作废") is True
        assert h.get(rid)["status"] == "invalid"

    def test_mark_invalid_nonexistent(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        assert h.mark_invalid(999) is False


class TestCLIReview:
    """CLI: qs review list/approve/reject"""

    def _make_app_mock(self, env):
        db, pool, pm = env
        app = MagicMock()
        app.execution.plan_manager = pm
        return app

    def test_review_list_pending(self, env):
        import cli.main as m
        db, pool, pm = env
        pm.human_in_loop.evaluate("000001", "buy", "待审", 0.8, 0.6,
                                  entry_low=10.0, entry_high=11.0)
        runner = CliRunner()
        with patch.object(m, "_get_human_in_loop", return_value=(pm, pm.human_in_loop)):
            result = runner.invoke(m.cli, ["review", "list"])
        assert result.exit_code == 0
        assert "000001" in result.output
        assert "pending" in result.output

    def test_review_approve_consumes(self, env):
        import cli.main as m
        db, pool, pm = env
        pm.human_in_loop.evaluate("000001", "buy", "批准我", 0.8, 0.6,
                                  entry_low=10.0, entry_high=11.0,
                                  stop_loss=9.0, take_profit=13.0)
        rid = pm.human_in_loop.get_pending()[0]["id"]
        runner = CliRunner()
        with patch.object(m, "_get_human_in_loop", return_value=(pm, pm.human_in_loop)):
            result = runner.invoke(m.cli, ["review", "approve", str(rid)])
        assert result.exit_code == 0
        assert "已批准" in result.output
        assert pool.get("000001")["status"] == "pending_entry"

    def test_review_reject(self, env):
        import cli.main as m
        db, pool, pm = env
        pm.human_in_loop.evaluate("000001", "buy", "拒绝我", 0.8, 0.6)
        rid = pm.human_in_loop.get_pending()[0]["id"]
        runner = CliRunner()
        with patch.object(m, "_get_human_in_loop", return_value=(pm, pm.human_in_loop)):
            result = runner.invoke(m.cli, ["review", "reject", str(rid), "--note", "不要"])
        assert result.exit_code == 0
        assert "已拒绝" in result.output
        assert pm.human_in_loop.get(rid)["status"] == "rejected"

    def test_review_approve_nonexistent_fails(self, env):
        import cli.main as m
        db, pool, pm = env
        runner = CliRunner()
        with patch.object(m, "_get_human_in_loop", return_value=(pm, pm.human_in_loop)):
            result = runner.invoke(m.cli, ["review", "approve", "999"])
        assert result.exit_code == 0
        assert "批准失败" in result.output


class TestPendingReviewPush:
    """EX-01: pending 审核经 delivery 层推送飞书卡片 (只推送)"""

    def test_require_review_pushes_feishu_card(self, env):
        db, pool, _ = env
        delivery = MagicMock()
        pm = PlanManager(db, pool, ActionRecord(db), delivery=delivery)
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.6, "reason": "中等置信", "initial_score": 70,
             "entry_low": 10.0, "entry_high": 11.0},
        ]})
        # 进了审核队列
        assert len(pm.human_in_loop.get_pending()) == 1
        # 推送了飞书卡片
        assert delivery.send.called
        msg = delivery.send.call_args[0][0]
        assert msg.content_type == "execution_alert"
        assert "000001" in str(msg.content)

    def test_auto_approve_no_push(self, env):
        db, pool, _ = env
        delivery = MagicMock()
        pm = PlanManager(db, pool, ActionRecord(db), delivery=delivery)
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.9, "reason": "高置信", "initial_score": 90,
             "entry_low": 10.0, "entry_high": 11.0},
        ]})
        assert not delivery.send.called  # 自动批准不推送

    def test_push_failure_does_not_block(self, env):
        db, pool, _ = env
        delivery = MagicMock()
        delivery.send.side_effect = RuntimeError("feishu down")
        pm = PlanManager(db, pool, ActionRecord(db), delivery=delivery)
        # 推送失败不阻塞审核入队
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.6, "reason": "中等置信", "initial_score": 70,
             "entry_low": 10.0, "entry_high": 11.0},
        ]})
        assert len(pm.human_in_loop.get_pending()) == 1


class TestHitlDeliveryLog:
    """卡②: feishu_hitl_dm 发卡必须落 delivery_log (铁律#12 可观测不可静默)。

    根因: _get_delivery() 构造 DeliveryLayer(config=cfg) 未传 db →
    executor.log_store=None → HITL 审批/通知卡发送全程不落 delivery_log。
    修法: 传 db=self.db (对齐 bootstrap.py DeliveryLayer(config, db=database))。
    """

    def _pm(self, tmp_path):
        db = Database(str(tmp_path / "hitl_dlog.db"))
        pool = StockPool(db)
        pool._ensure_table()
        ar = ActionRecord(db)
        ar._ensure_table()
        return PlanManager(db, pool, ar), db

    def test_get_delivery_wires_log_store(self, tmp_path, monkeypatch):
        """_get_delivery 构造的 DeliveryLayer 必须接 db → executor.log_store 非 None"""
        from config_layer import Config
        pm, db = self._pm(tmp_path)
        fake_cfg = Config.__new__(Config)
        fake_cfg._loaded = True
        monkeypatch.setattr("config_layer.get_config", lambda: fake_cfg)
        delivery = pm._get_delivery()
        assert delivery is not None
        assert delivery.executor.log_store is not None, (
            "DeliveryLayer 未接 db → HITL 卡 delivery_log 静默丢失 (违铁律#12)")

    def test_hitl_card_send_lands_delivery_log(self, tmp_path, monkeypatch):
        """端到端: pending 审核推 HITL 卡 → delivery_log 落一行 success"""
        from config_layer import Config
        from delivery_layer.channels import feishu_hitl_dm
        pm, db = self._pm(tmp_path)
        fake_cfg = Config.__new__(Config)
        fake_cfg._loaded = True
        monkeypatch.setattr("config_layer.get_config", lambda: fake_cfg)
        # HITL 通道凭据 + 出站全打桩 (不打真飞书)
        for k, v in {"FEISHU_HITL_APP_ID": "cli_new",
                     "FEISHU_HITL_APP_SECRET": "s_new",
                     "FEISHU_HITL_DM_UNION_ID": "on_union"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(feishu_hitl_dm, "_get_cached_token", lambda a, s: "tok")

        class R:
            @staticmethod
            def json():
                return {"code": 0, "data": {"message_id": "om_hitl"}}
        monkeypatch.setattr(feishu_hitl_dm.requests, "post",
                            lambda *a, **k: R)
        # 触发 pending 审核推送 (走真实 DeliveryLayer, 非 MagicMock)
        pm.receive_plan({"recommendations": [
            {"code": "000001", "name": "测试", "action": "buy",
             "confidence": 0.6, "reason": "中等置信", "initial_score": 70,
             "entry_low": 10.0, "entry_high": 11.0},
        ]})
        rows = db.get_connection().execute(
            "SELECT channel, status FROM delivery_log").fetchall()
        assert any(r[0] == "feishu_hitl_dm" and r[1] == "success" for r in rows), (
            f"HITL 卡发送未落 delivery_log, 现有行: {rows}")
