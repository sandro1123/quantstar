"""Tests for AgentQualityRepository"""
import pytest
from common.database import Database
from agent_layer.quality.repository import AgentQualityRepository


@pytest.fixture
def repo():
    d = Database(":memory:")
    r = AgentQualityRepository(d)
    r.ensure_tables()
    return r


class TestAgentQualityRepository:
    def test_ensure_tables(self, repo):
        conn = repo.db.get_connection()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "agent_predictions" in tables
        assert "agent_memory" in tables

    def test_write_and_read_prediction(self, repo):
        repo.write_prediction("req1", "analyst", "000001.SZ", "2026-01-01", "推荐", 0.8)
        rows = repo.get_predictions_by_date("2026-01-01")
        assert len(rows) == 1
        assert rows[0]["code"] == "000001.SZ"

    def test_update_prediction_result(self, repo):
        repo.write_prediction("req1", "a", "000001.SZ", "2026-01-01", "推荐", 0.8)
        rows = repo.get_predictions_by_date("2026-01-01")
        repo.update_prediction_result(rows[0]["id"], 0.05, 1)
        assert len(repo.get_unresolved_predictions("2026-01-01")) == 0

    def test_get_unresolved(self, repo):
        repo.write_prediction("r1", "a", "001.SZ", "2026-01-01", "推荐", 0.8)
        repo.write_prediction("r2", "a", "002.SZ", "2026-01-01", "不推荐", 0.6)
        assert len(repo.get_unresolved_predictions("2026-01-01")) == 2

    def test_accuracy_stats_empty(self, repo):
        stats = repo.get_accuracy_stats()
        assert stats["total"] == 0

    def test_accuracy_stats_with_data(self, repo):
        repo.write_prediction("r1", "analyst", "001.SZ", "2026-01-01", "推荐", 0.8)
        repo.update_prediction_result(1, 0.05, 1)
        stats = repo.get_accuracy_stats("analyst", days=30)
        assert stats["total"] == 1
        assert stats["hits"] == 1

    def test_get_accuracy_sum(self, repo):
        from datetime import date
        today = date.today().isoformat()
        repo.write_prediction("r1", "analyst", "001.SZ", today, "推荐", 0.8)
        repo.update_prediction_result(1, 0.05, 1)
        total, hits = repo.get_accuracy_sum("analyst", days=30)
        assert total == 1
        assert hits == 1

    def test_memory_remember_and_recall(self, repo):
        repo.memory_remember("001.SZ", "2026-01-01", "推荐", 0.8, "good")
        result = repo.memory_recall("001.SZ")
        assert result is not None
        assert result["verdict"] == "推荐"

    def test_memory_recall_history(self, repo):
        repo.memory_remember("001.SZ", "2026-01-01", "推荐", 0.8)
        repo.memory_remember("001.SZ", "2026-01-02", "不推荐", 0.6)
        history = repo.memory_recall_history("001.SZ", limit=1)
        assert len(history) == 1

    def test_no_db_returns_safe_defaults(self):
        r = AgentQualityRepository(None)
        r.ensure_tables()
        assert r.get_predictions_by_date("2026-01-01") == []
        assert r.get_unresolved_predictions("2026-01-01") == []
        assert r.get_accuracy_stats() == {"total": 0, "hits": 0, "accuracy": 0.0}
        assert r.get_accuracy_sum() == (0, 0)
        assert r.memory_recall("001.SZ") is None
        assert r.memory_recall_history("001.SZ") == []
