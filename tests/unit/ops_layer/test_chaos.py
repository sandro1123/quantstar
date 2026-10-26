"""Tests for ChaosEngine validate_identifier protection"""
from common.database import Database
from ops_layer.chaos import ChaosEngine


class TestChaosEngine:
    def test_simulate_database_corruption_rejects_invalid_table(self):
        engine = ChaosEngine(Database(":memory:"))
        result = engine.simulate_database_corruption("daily_klines; DROP TABLE users--")
        assert result["status"] == "failed"
        assert "Invalid" in result["detail"]

    def test_simulate_database_corruption_accepts_valid_table(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE chaos_test_table (id INTEGER)")
        conn.commit()
        engine = ChaosEngine(db)
        result = engine.simulate_database_corruption("chaos_test_table")
        assert result["status"] == "corrupted"

    def test_simulate_database_corruption_rejects_empty_table(self):
        engine = ChaosEngine(Database(":memory:"))
        result = engine.simulate_database_corruption("")
        assert result["status"] == "failed"

    def test_simulate_database_corruption_rejects_table_with_spaces(self):
        engine = ChaosEngine(Database(":memory:"))
        result = engine.simulate_database_corruption("my table")
        assert result["status"] == "failed"