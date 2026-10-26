"""Release 2 集成测试"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.stats.display import ActionFeed
from delivery_layer import DeliveryLayer, Message
from report_layer import ReportLayer
from config_layer import get_config, reset_config


def test_execution_delivery_integration():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    el.stock_pool.add("000001", "平安银行", "manual", "测试", 85)
    el.stock_pool.update_status("000001", "active")
    el.stock_pool.update("000001", entry_price=15.0, entry_date="2026-07-17")
    snap = el.get_portfolio_snapshot()
    assert snap.active == 1
    actions = el.get_today_actions()
    assert isinstance(actions, ActionFeed)
    el.shutdown()
    db.close()


def test_delivery_message():
    dl = DeliveryLayer()
    m = Message(content={"sections": []}, content_type="test", title="测试")
    results = dl.send(m, "debug")
    assert isinstance(results, list)
    dl.shutdown()


def test_report_generation():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    rl = ReportLayer(execution_layer=el)
    msg = rl.build_evening_report("2026-07-17", "debug")
    assert msg is not None
    assert msg.content_type == "evening_report"
    el.shutdown()
    db.close()