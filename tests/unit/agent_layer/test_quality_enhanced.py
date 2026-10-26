"""质量追踪增强测试 — Evaluator / Calibrator / Memory / QuotaManager"""
import pytest
from common.database import Database
from agent_layer.quality.evaluator import Evaluator
from agent_layer.quality.calibrator import Calibrator
from agent_layer.quality.memory import AgentMemory
from agent_layer.llm.quota_manager import QuotaManager
from agent_layer.llm.mock_provider import MockProvider


def test_evaluator_empty():
    db = Database(":memory:")
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {})
    assert result["evaluated"] == 0


def test_evaluator_with_predictions():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000002', '2026-07-17', '不推荐', 0.7)")
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05, "000002": -0.03})
    assert result["evaluated"] == 2
    assert result["hits"] >= 0


def test_quality_tracker_neutral_verdict():
    """AG-03: 中性判决（观望/警惕）不计分; 存量旧词"关注"(旧quick_scan正向结论)归一化为推荐并计分"""
    from agent_layer.quality.tracker import QualityTracker
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '观望', 0.5)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000002', '2026-07-17', '警惕', 0.6)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000003', '2026-07-17', '推荐', 0.8)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000004', '2026-07-17', '关注', 0.6)")
    conn.commit()
    qt = QualityTracker(db=db)
    qt.evaluate("2026-07-17", {"000001": 0.05, "000002": -0.02, "000003": 0.03, "000004": 0.04})
    rows = conn.execute("SELECT verdict, hit FROM agent_predictions WHERE date = '2026-07-17' ORDER BY id").fetchall()
    assert len(rows) == 4
    assert rows[0]["hit"] is None, "观望应标记为 NULL（不计分）"
    assert rows[1]["hit"] is None, "警惕应标记为 NULL（不计分）"
    assert rows[2]["hit"] == 1, "推荐+上涨应标记为命中"
    assert rows[3]["hit"] == 1, "存量旧词'关注'归一化为推荐, +上涨应命中"
    db.close()


def test_calibrator_no_db():
    cal = Calibrator(database=None)
    assert cal.calibrate("test", 0.8) == 0.8


def test_calibrator_adjusts():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, verdict TEXT, confidence REAL, hit INTEGER)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence, hit) VALUES ('000001', '2026-07-17', '推荐', 0.8, 1)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence, hit) VALUES ('000002', '2026-07-17', '推荐', 0.7, 0)")
    conn.commit()
    cal = Calibrator(database=db)
    calibrated = cal.calibrate("default", 0.8)
    assert 0.05 <= calibrated <= 0.95



def test_calibrator_no_eval_data_passthrough():
    """AG-15 段1: hit 全 NULL (无可评估样本) → 透传原置信度, 不再虚构 0.5 折扣."""
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, verdict TEXT, confidence REAL, hit INTEGER)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.commit()
    cal = Calibrator(database=db)
    assert cal.get_accuracy() is None
    assert cal.calibrate("default", 0.8) == 0.8


def test_calibrator_query_error_passthrough():
    """AG-15 段1: 查询异常 → 透传 + 不抛出 (异常也不冒充)."""
    from unittest.mock import patch
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, verdict TEXT, confidence REAL, hit INTEGER)")
    conn.commit()
    cal = Calibrator(database=db)
    with patch.object(cal.repo, "get_accuracy_sum", side_effect=RuntimeError("boom")):
        assert cal.get_accuracy() is None
        assert cal.calibrate("default", 0.8) == 0.8

def test_memory_save_recall():
    db = Database(":memory:")
    mem = AgentMemory(database=db)
    mem.remember("000001", "2026-07-17", "推荐", 0.8, "基本面好")
    rec = mem.recall("000001")
    assert rec is not None
    assert rec["verdict"] == "推荐"
    history = mem.recall_history("000001")
    assert len(history) == 1


def test_quota_manager_register():
    qm = QuotaManager()
    qm.register("openrouter", total=1000)
    assert qm.remaining("openrouter") > 0


def test_quota_manager_select():
    qm = QuotaManager()
    qm.register("provider_a", total=100)
    qm.register("provider_b", total=200)
    selected = qm.select("any_task")
    assert selected in ("provider_a", "provider_b")


def test_quota_manager_exhausted():
    qm = QuotaManager()
    qm.register("limited", total=5)
    for _ in range(10):
        qm.record_usage("limited")
    selected = qm.select("any_task")
    assert selected is None


def test_mock_provider():
    mock = MockProvider()
    mock.register_response("000001", '{"verdict": "推荐", "confidence": 0.9}')
    result = mock.chat("分析000001")
    assert result is not None
    assert "推荐" in result


def test_mock_provider_structured():
    mock = MockProvider()
    mock.register_response("000001", '{"verdict": "推荐", "confidence": 0.9}')
    result = mock.chat_structured("分析000001")
    assert result is not None
    assert result["verdict"] == "推荐"


def test_mock_provider_default():
    mock = MockProvider()
    result = mock.chat("未知股票")
    assert result is not None