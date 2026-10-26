"""Tests for execution layer remaining modules"""
from common.database import Database
from execution_layer.analysis.base_analyzer import BaseAnalyzer
from execution_layer.intraday.monitor import IntradayMonitor
from unittest.mock import MagicMock


class TestBaseAnalyzer:
    def test_analyzer(self):
        class TestAnalyzer(BaseAnalyzer):
            name = "test"
            def analyze(self):
                return {"status": "ok", "value": 42}
        a = TestAnalyzer(db=Database(":memory:"))
        r = a.analyze()
        assert r["status"] == "ok"
        assert r["value"] == 42


class TestIntradayMonitor:
    def test_interval_changes_by_time(self):
        monitor = IntradayMonitor(Database(":memory:"), MagicMock())
        assert monitor._get_interval_for_time("09:35:00") == 120
        assert monitor._get_interval_for_time("11:00:00") == 300
        assert monitor._get_interval_for_time("14:45:00") == 60