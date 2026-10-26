"""Tests for pluggable analyzer architecture (G-E12) + SQN analyzer (G-E13)"""
from unittest.mock import MagicMock, patch

import pytest
from execution_layer.analysis.base_analyzer import BaseAnalyzer
from execution_layer.analysis.analyzers import SQNAnalyzer


def test_base_analyzer_abstract():
    with pytest.raises(TypeError):
        BaseAnalyzer(MagicMock())


def test_base_analyzer_abstract_methods():
    class IncompleteAnalyzer(BaseAnalyzer):
        pass
    with pytest.raises(TypeError):
        IncompleteAnalyzer(MagicMock())


def test_concrete_analyzer():
    class ConcreteAnalyzer(BaseAnalyzer):
        def name(self) -> str:
            return "Test"

        def analyze(self) -> dict:
            return {"result": 42}

    inst = ConcreteAnalyzer(MagicMock())
    assert inst.name() == "Test"
    assert inst.analyze() == {"result": 42}


def test_sqn_analyzer_insufficient_data():
    db = MagicMock()
    db.get_connection.return_value.execute.return_value.fetchall.return_value = [
        {"total_return_pct": 5.0} for _ in range(5)
    ]
    analyzer = SQNAnalyzer(db)
    result = analyzer.analyze()
    assert result["sqn"] == 0
    assert result["quality"] == "样本不足"
    assert result["num_trades"] == 5


def test_sqn_analyzer_normal():
    db = MagicMock()
    db.get_connection.return_value.execute.return_value.fetchall.return_value = [
        {"total_return_pct": 2.0 + i * 0.5} for i in range(20)
    ]
    analyzer = SQNAnalyzer(db)
    result = analyzer.analyze()
    assert "sqn" in result
    assert result["num_trades"] == 20
    assert result["sqn"] > 0


def test_sqn_analyzer_quality_tiers():
    db = MagicMock()

    def mock_returns(returns):
        db.get_connection.return_value.execute.return_value.fetchall.return_value = [
            {"total_return_pct": r} for r in returns
        ]

    mock_returns([0.1] * 10)
    assert SQNAnalyzer(db).analyze()["quality"] == "较差"

    mock_returns([2.0] * 10)
    q = SQNAnalyzer(db).analyze()["quality"]
    assert q in ("一般", "良好", "优秀")

    mock_returns([10.0] * 10)
    assert SQNAnalyzer(db).analyze()["quality"] == "优秀"


def test_sqn_analyzer_name():
    db = MagicMock()
    analyzer = SQNAnalyzer(db)
    assert "SQN" in analyzer.name()


def test_analyzers_in_execution_layer():
    from execution_layer import ExecutionLayer
    with patch("execution_layer.ExecutionLayer.__init__", lambda self: None):
        el = ExecutionLayer()
        el.analyzers = {"sqn": MagicMock()}
        result = el.run_analyzer("sqn")
        assert result is not None


def test_list_analyzers():
    from execution_layer.analysis.analyzers import SQNAnalyzer
    from common.database import Database
    db = MagicMock(spec=Database)
    analyzers = {"sqn": SQNAnalyzer(db)}
    assert "sqn" in analyzers