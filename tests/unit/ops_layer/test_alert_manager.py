"""Tests for ops_layer/alert_manager.py"""
import pytest
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import Alert


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def test_send_alert(db):
    am = AlertManager(db)
    alert = Alert(severity="critical", layer="data", title="db down", detail="cannot connect")
    result = am.send(alert)
    assert result is True


def test_send_alert_persists(db):
    am = AlertManager(db)
    alert = Alert(severity="warning", layer="strategy", title="model drift", detail="ic dropped")
    am.send(alert)
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM alerts WHERE title='model drift'").fetchone()
    assert row is not None
    assert row["severity"] == "warning"
    assert row["status"] == "open"


def test_get_open_alerts(db):
    am = AlertManager(db)
    am.send(Alert(severity="critical", layer="data", title="db down", detail="cannot connect"))
    am.send(Alert(severity="info", layer="ops", title="deploy", detail="v1.0 deployed"))
    alerts = am.get_open_alerts()
    assert len(alerts) == 2


def test_dedup_within_30min(db):
    am = AlertManager(db)
    alert1 = Alert(severity="warning", layer="data", title="disk low", detail="90%")
    am.send(alert1)
    alert2 = Alert(severity="warning", layer="data", title="disk low", detail="91%")
    result = am.send(alert2)
    assert result is False
    conn = db.get_connection()
    count = conn.execute("SELECT COUNT(*) FROM alerts WHERE title='disk low'").fetchone()[0]
    assert count == 1


def test_open_alert_suppressed_past_window(db):
    """P0-3②: 持续期抑制不限时长 (旧 30min 重推已废); resolve 后才重新首触发"""
    am = AlertManager(db)
    alert = Alert(severity="warning", layer="data", title="disk low", detail="90%")
    am.send(alert)
    # 把 open 告警创建时间回拨 2 小时 — 旧语义会重推, 状态机语义仍抑制
    conn = db.get_connection()
    conn.execute("UPDATE alerts SET created_at='2020-01-01 00:00:00' WHERE title='disk low'")
    conn.commit()
    am._recent.clear()
    alert2 = Alert(severity="warning", layer="data", title="disk low", detail="95%")
    assert am.send(alert2) is False
    count = conn.execute("SELECT COUNT(*) FROM alerts WHERE title='disk low'").fetchone()[0]
    assert count == 1


def test_push_feishu_skips_without_credentials(db):
    """S0 回归: 凭据缺失时 push_feishu 优雅降级(仅落库)不抛异常"""
    import os
    from unittest.mock import patch
    am = AlertManager(db)
    env = {k: "" for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DM_ID")}
    with patch.dict(os.environ, env, clear=False):
        assert am.push_feishu("t", "b", severity="critical") is False


def test_send_calls_push_feishu(db):
    """S0 回归: send 落库后触发飞书推送(mock 验证调用链)"""
    from unittest.mock import patch
    am = AlertManager(db)
    with patch.object(am, "push_feishu", return_value=True) as m:
        alert = Alert(severity="critical", layer="ops", title="pipeline down", detail="x")
        assert am.send(alert) is True
        m.assert_called_once()
        args, kwargs = m.call_args
        assert "pipeline down" in args[0]
        assert kwargs.get("severity") == "critical"


def test_cross_process_dedup_via_db(db):
    """S0 回归: 新进程无 in-memory 记录时, 用 DB 最近 open 告警时间做 30min 去重"""
    am = AlertManager(db)
    alert = Alert(severity="warning", layer="data", title="disk low", detail="90%")
    assert am.send(alert) is True

    am2 = AlertManager(db)
    assert am2._recent == {}
    dup = Alert(severity="warning", layer="data", title="disk low", detail="91%")
    assert am2.should_alert(dup) is False
