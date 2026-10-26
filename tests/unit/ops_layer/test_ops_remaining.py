"""Tests for ops layer remaining modules"""
from common.database import Database
from ops_layer.anomaly_detector import AnomalyDetector
from ops_layer.auto_healer import AutoHealer


class TestAnomalyDetector:
    def test_detect_invalid_table_name(self):
        ad = AnomalyDetector(Database(":memory:"))
        result = ad.check_data_volume_anomaly("invalid table; drop", 100)
        assert result is None


class TestAutoHealer:
    def test_heal_skips_invalid_table(self):
        ah = AutoHealer(Database(":memory:"))
        result = ah._heal_purge_old_data({"table": "invalid; drop"})
        assert result["status"] == "skipped"

    def test_heal_accepts_valid_table(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_valid (created_at TEXT)")
        conn.commit()
        ah = AutoHealer(db)
        result = ah._heal_purge_old_data({"table": "test_valid"})
        assert result["status"] == "success"