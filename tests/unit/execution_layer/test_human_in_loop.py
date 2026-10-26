"""Tests for HumanInLoop"""
from common.database import Database
from execution_layer.core.human_in_loop import HumanInLoop


class TestHumanInLoop:
    def test_evaluate_auto_approves_high_confidence(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        result = h.evaluate("000001", "buy", "good", 0.8, 0.85)
        assert result.action == "auto_approve"

    def test_evaluate_requires_review_for_medium_confidence(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        result = h.evaluate("000001", "buy", "ok", 0.5, 0.6)
        assert result.action == "require_review"

    def test_evaluate_auto_rejects_low_confidence(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        result = h.evaluate("000001", "buy", "bad", 0.2, 0.3)
        assert result.action == "auto_reject"

    def test_evaluate_adds_to_queue_when_review_required(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "review me", 0.5, 0.6)
        pending = h.get_pending()
        assert len(pending) == 1
        assert pending[0]["code"] == "000001"

    def test_evaluate_does_not_add_to_queue_for_auto(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "auto", 0.8, 0.85)
        pending = h.get_pending()
        assert len(pending) == 0

    def test_approve_updates_status(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "approve me", 0.5, 0.6)
        pending = h.get_pending()
        assert h.approve(pending[0]["id"], reviewer="test") is True
        pending_after = h.get_pending()
        assert len(pending_after) == 0

    def test_reject_updates_status(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "reject me", 0.5, 0.6)
        pending = h.get_pending()
        assert h.reject(pending[0]["id"], reviewer="test") is True
        pending_after = h.get_pending()
        assert len(pending_after) == 0

    def test_get_stats(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        h.evaluate("000001", "buy", "review1", 0.5, 0.6)
        h.evaluate("000002", "buy", "review2", 0.5, 0.55)
        stats = h.get_stats()
        assert stats["total"] == 2
        assert stats["pending"] == 2
        assert stats["auto_approve_rate"] >= 0

    def test_falls_back_to_buy_for_unknown_type(self):
        db = Database(":memory:")
        h = HumanInLoop(db)
        result = h.evaluate("000001", "unknown_type", "test", 0.5, 0.6)
        assert result.action == "require_review"