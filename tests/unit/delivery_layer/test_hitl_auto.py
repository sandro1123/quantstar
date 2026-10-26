"""HITL Phase 1.6: 自动审批模式自测 (mock 出站, 不打真飞书)

覆盖: veto 状态机 / 否决三态联动 / 自动批准分支 (开关 on/off) /
通知卡模板 / ledger 抽取 / 清算批次定义。
"""
import contextlib
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.database import Database
from delivery_layer import hitl_ws_daemon as daemon
from execution_layer.core.human_in_loop import HumanInLoop
from execution_layer.core.plan_manager import PlanManager
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.stock_pool import StockPool


def _event(action, review_id, user_id="sandro", message_id="om_card"):
    return json.dumps({
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": "tk",
                   "event_id": "ev1", "app_id": "cli_test"},
        "event": {
            "operator": {"open_id": "ou_abc", "user_id": user_id},
            "action": {"tag": "button",
                       "value": {"action": action, "review_id": str(review_id)}},
            "context": {"open_message_id": message_id, "open_chat_id": "oc_1"},
        },
    }).encode("utf-8")


@pytest.fixture
def hitl(tmp_path, monkeypatch):
    """内存库 + 出站网络全打桩 (同 test_hitl_ws.py 口径)"""
    h = HumanInLoop(Database(":memory:"))

    @contextlib.contextmanager
    def _session():
        yield h

    monkeypatch.setattr(daemon, "hitl_session", _session)
    monkeypatch.setattr(daemon, "HEARTBEAT_PATH", str(tmp_path / "hb.jsonl"))

    calls = {"patch": [], "ledger": []}
    monkeypatch.setattr(daemon, "patch_card",
                        lambda mid, card: calls["patch"].append((mid, card)) or True)
    monkeypatch.setattr(daemon, "write_bitable_ledger",
                        lambda *a, **k: calls["ledger"].append(a) or True)
    h.calls = calls
    h.hb_path = str(tmp_path / "hb.jsonl")
    return h


def _seed(h, code="000001"):
    v = h.evaluate(code, "buy", "放量突破", 82.5, 0.62,
                   entry_low=12.5, entry_high=13.0,
                   stop_loss=11.8, take_profit=14.5)
    assert v.action == "require_review"
    return v.review_id


class TestVetoStateMachine:
    def test_veto_flips_approved_to_rejected(self, hitl):
        rid = _seed(hitl)
        assert hitl.approve(rid, reviewer="auto-paper")
        assert hitl.veto(rid, reviewer="sandro")
        row = hitl.get(rid)
        assert row["status"] == "rejected"
        assert row["reviewer"] == "sandro"
        assert "否决" in (row["review_note"] or "")

    def test_veto_on_pending_fails(self, hitl):
        rid = _seed(hitl)
        assert hitl.veto(rid) is False
        assert hitl.get(rid)["status"] == "pending"

    def test_veto_idempotent(self, hitl):
        rid = _seed(hitl)
        hitl.approve(rid, reviewer="auto-paper")
        assert hitl.veto(rid, reviewer="sandro") is True
        assert hitl.veto(rid, reviewer="other") is False
        assert hitl.get(rid)["reviewer"] == "sandro", "二次否决不得改写审批人"

    def test_veto_hits_approved_even_when_consumed(self, hitl):
        rid = _seed(hitl)
        hitl.approve(rid, reviewer="auto-paper")
        hitl.mark_consumed(rid)
        assert hitl.veto(rid) is True
        assert hitl.get(rid)["status"] == "rejected"


class TestVetoCallback:
    def test_veto_via_card_callback(self, hitl):
        rid = _seed(hitl)
        hitl.approve(rid, reviewer="auto-paper")
        resp = daemon.build_handler()._do_without_validation(_event("veto", rid))
        assert hitl.get(rid)["status"] == "rejected"
        assert resp.toast.type == "success"
        # 台账记 veto 动作
        assert hitl.calls["ledger"][0][3] == "veto"

    def test_veto_pending_is_noop(self, hitl):
        rid = _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("veto", rid))
        assert hitl.get(rid)["status"] == "pending"
        assert resp.toast.type == "info"
        assert not hitl.calls["ledger"]

    def test_veto_result_card_has_no_buttons(self, hitl):
        rid = _seed(hitl)
        hitl.approve(rid, reviewer="auto-paper")
        daemon.build_handler()._do_without_validation(_event("veto", rid))
        _, card = hitl.calls["patch"][0]
        assert card["header"]["template"] == "red"
        assert "已否决" in card["header"]["title"]["content"]
        assert "action" not in [e.get("tag") for e in card["elements"]]


class TestVetoSideEffects:
    """三态联动用文件库跑真 StockPool (内存库跨连接不共享)"""

    def _pool(self, tmp_path, monkeypatch, status):
        db_path = str(tmp_path / "veto.db")
        monkeypatch.setattr(daemon, "Database", lambda: Database(db_path))
        db = Database(db_path)
        pool = StockPool(db)
        pool.add(code="000001", name="平安银行", source="strategy",
                 reason="t", score=80)
        pool.update_status("000001", status)
        return db_path

    def test_not_consumed_no_side_effect(self, hitl):
        assert daemon._apply_veto_side_effects("000001", 1, consumed=False) == "not_consumed"

    def test_pending_entry_reverts_to_watch(self, hitl, tmp_path, monkeypatch):
        self._pool(tmp_path, monkeypatch, "pending_entry")
        assert daemon._apply_veto_side_effects("000001", 1, consumed=True) == "entry_cancelled"
        pool = StockPool(Database(str(tmp_path / "veto.db")))
        assert pool.get("000001")["status"] == "watch"

    def test_active_triggers_exit(self, hitl, tmp_path, monkeypatch):
        self._pool(tmp_path, monkeypatch, "active")
        assert daemon._apply_veto_side_effects("000001", 1, consumed=True) == "exit_triggered"
        pool = StockPool(Database(str(tmp_path / "veto.db")))
        assert pool.get("000001")["status"] == "exit_pending"

    def test_missing_pool_row(self, hitl, tmp_path, monkeypatch):
        db_path = str(tmp_path / "veto.db")
        monkeypatch.setattr(daemon, "Database", lambda: Database(db_path))
        assert daemon._apply_veto_side_effects("999999", 1, consumed=True) == "no_pool_row"


class TestAutoApproveBranch:
    def _pm(self, hitl, monkeypatch, auto: bool):
        monkeypatch.setattr(PlanManager, "_auto_approve_enabled", lambda self: auto)
        pm = PlanManager(hitl.db, MagicMock(), MagicMock())
        pm.human_in_loop = hitl
        pm._delivery_ready = True
        pm._delivery = MagicMock()
        pm._delivery.send = MagicMock(return_value=[])
        monkeypatch.setattr("delivery_layer.hitl_ledger.write_bitable_ledger",
                            lambda *a, **k: True)
        return pm

    def _plan(self, confidence=0.62):
        return {"recommendations": [{
            "code": "000001", "name": "平安银行", "action": "buy",
            "confidence": confidence, "reason": "放量突破", "initial_score": 82.5,
            "entry_low": 12.5, "entry_high": 13.0,
            "stop_loss": 11.8, "take_profit": 14.5}]}

    def test_auto_mode_approves_and_notifies(self, hitl, monkeypatch):
        pm = self._pm(hitl, monkeypatch, auto=True)
        pm.receive_plan(self._plan())
        rows = [r for r in hitl.get_pending()]
        assert rows == [], "auto 模式下不应残留 pending"
        conn = hitl.db.get_connection()
        row = conn.execute("SELECT * FROM human_review_queue").fetchone()
        assert row["status"] == "approved"
        assert row["reviewer"] == "auto-paper"
        # 通知卡: 绿头 + 单否决按钮, DM 锁死
        msg = pm._delivery.send.call_args[0][0]
        assert msg.force_channel == "feishu_hitl_dm"  # Phase 2: HITL 卡走新 app 通道
        card = msg.content["card"]
        assert card["header"]["template"] == "green"
        actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
        assert len(actions) == 1
        assert actions[0]["value"]["action"] == "veto"

    def test_manual_mode_unchanged(self, hitl, monkeypatch):
        pm = self._pm(hitl, monkeypatch, auto=False)
        pm.receive_plan(self._plan())
        assert len(hitl.get_pending()) == 1, "manual 模式保持 pending 等人工"
        card = pm._delivery.send.call_args[0][0].content["card"]
        assert card["header"]["template"] == "orange", "manual 模式仍发审批卡"

    def test_auto_mode_high_confidence_bypasses_queue(self, hitl, monkeypatch):
        pm = self._pm(hitl, monkeypatch, auto=True)
        pm.receive_plan(self._plan(confidence=0.9))
        conn = hitl.db.get_connection()
        assert conn.execute("SELECT COUNT(*) FROM human_review_queue").fetchone()[0] == 0
        # 直接进池 (既有 auto_approve 路径)
        pm.stock_pool.add.assert_called()

    def test_switch_read_failure_defaults_off(self, hitl, monkeypatch):
        def boom(key, default=None):
            raise RuntimeError("config not loaded")
        monkeypatch.setattr("config_layer.cfg_get", boom)
        pm = PlanManager(hitl.db, MagicMock(), MagicMock())
        assert pm._auto_approve_enabled() is False


class TestNotificationCardTemplate:
    def test_notification_card_shape(self):
        from delivery_layer.hitl_card_templates import build_notification_card
        card = build_notification_card(42, "000001", "buy", "放量突破", 82.5, 0.62,
                                       12.5, 13.0, 11.8, 14.5, name="平安银行")
        assert card["header"]["template"] == "green"
        assert "已自动批准" in card["header"]["title"]["content"]
        assert len(card["elements"][0]["fields"]) == 6
        actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
        assert [a["value"] for a in actions] == [{"action": "veto", "review_id": "42"}]


class TestLedgerExtraction:
    def test_daemon_reexports_ledger(self):
        from delivery_layer.hitl_ledger import write_bitable_ledger
        assert daemon.write_bitable_ledger is write_bitable_ledger

    def test_ledger_skips_without_config(self, monkeypatch):
        from delivery_layer import hitl_ledger
        monkeypatch.delenv("FEISHU_BITABLE_APPROVAL_TABLE", raising=False)
        monkeypatch.delenv("FEISHU_BITABLE_APP_TOKEN", raising=False)
        assert hitl_ledger.write_bitable_ledger(
            1, "000001", "buy", "approve", "auto-paper", "", "auto_paper", "s") is False


# #96: 测试加载脚本单点 — 原绝对路径指废 worktree (qs-hitl, 冻结 08-26, 停 5e84d88)
_SCRIPT = (Path(__file__).resolve().parents[3] / "scripts"
             / "hitl_backlog_liquidation.py")


class TestBacklogScript:
    def test_batch_definitions_match_measured_backlog(self):
        """批次 id 来自 08-27 生产库实测 — 改动需重新核对积压分布"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hitl_backlog_liquidation", str(_SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.EXPIRE_IDS == list(range(42, 51)) + list(range(71, 81))
        assert mod.AUTO_IDS == list(range(81, 90))
        assert not set(mod.EXPIRE_IDS) & set(mod.AUTO_IDS)

    def test_script_is_live_version_not_stale_snapshot(self):
        """#96 守卫: 上一测加载的必须是本 repo 活版 — md5 对拍 main 现版。

        原绝对路径指废 worktree (qs-hitl, 冻结 08-26, 停 5e84d88), 该快照
        与 main 现版 (7d33338: feishu_dm→feishu_hitl_dm Phase2) md5 不一致
        — 而批次断言两版皆同, 旧形态一路假绿。本守卫固化 md5 对拍铁律:
        错指他 checkout / 回归绝对路径 / 脚本真改动未对拍时即红。
        """
        import subprocess
        _root = Path(__file__).resolve().parents[3]
        live_md5 = hashlib.md5(_SCRIPT.read_bytes()).hexdigest()
        main_body = subprocess.run(
            ["/usr/bin/git", "show", "HEAD:scripts/hitl_backlog_liquidation.py"],
            capture_output=True, check=True, cwd=str(_root)).stdout
        assert live_md5 == hashlib.md5(main_body).hexdigest(), (
            f"#96 旧快照假绿: 测试所指脚本 != main 现版 ({_SCRIPT}) — 路径须相对本 repo")
        # 7d33338 行为锁: Phase2 HITL 流量走新 app — 改回 feishu_dm 即红
        assert 'force_channel="feishu_hitl_dm"' in _SCRIPT.read_text(encoding="utf-8"), (
            "#96 行为锁: force_channel 现值应为 feishu_hitl_dm (7d33338 Phase2)")
