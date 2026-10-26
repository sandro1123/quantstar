"""P0-3②: 告警去重状态机 — 首触发/持续/恢复三态 (Monit 状态迁移语义)"""
from unittest.mock import patch

import pytest

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import Alert
from ops_layer.core.tables import OPS_TABLES_DDL


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def _alert(title="管道失败: ddp", detail="x"):
    return Alert(severity="warning", layer="ops", title=title, detail=detail)


def test_first_trigger_inserts_and_pushes(db):
    """首触发: 无同名 open → 落库 + 推送"""
    am = AlertManager(db)
    with patch.object(am, "push_feishu", return_value=True) as m:
        assert am.send(_alert()) is True
        m.assert_called_once()
    rows = db.get_connection().execute(
        "SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
    assert rows == 1


def test_ongoing_suppressed_no_row_no_push(db):
    """持续: 同名 open 已在 → 抑制, 不落新行不推送"""
    am = AlertManager(db)
    with patch.object(am, "push_feishu", return_value=True) as m:
        assert am.send(_alert()) is True
        assert am.send(_alert(detail="又失败了")) is False
        assert m.call_count == 1, "持续期不得重复推送"
    rows = db.get_connection().execute(
        "SELECT COUNT(*) FROM alerts WHERE title LIKE '%ddp%'").fetchone()[0]
    assert rows == 1, "持续期不得落新行"


def test_ongoing_suppressed_regardless_of_age(db):
    """持续期抑制不限时长 — open 告警再老也不重推 (旧 30min 窗口已废)"""
    am = AlertManager(db)
    am.send(_alert())
    conn = db.get_connection()
    conn.execute("UPDATE alerts SET created_at='2020-01-01 00:00:00'")
    conn.commit()
    am._recent.clear()
    assert am.should_alert(_alert()) is False


def test_refire_after_resolve_is_new_first_trigger(db):
    """恢复后再发 = 新首触发: 落新行 + 再推送"""
    am = AlertManager(db)
    with patch.object(am, "push_feishu", return_value=True) as m:
        assert am.send(_alert()) is True
        am.resolve_pipeline_alerts("ddp")  # 恢复 (推 recovery info)
        assert am.send(_alert(detail="再次失败")) is True
    rows = db.get_connection().execute(
        "SELECT COUNT(*) FROM alerts WHERE title LIKE '%ddp%'").fetchone()[0]
    assert rows == 2
    # 推送 = 首触发1 + 恢复1 + 新首触发1
    assert m.call_count == 3


def test_dismissed_also_rearms(db):
    """dismiss (误报驳回) 同样解除抑制 — 状态机看 open, 不看 resolved/dismissed 区别"""
    am = AlertManager(db)
    am.send(_alert())
    aid = db.get_connection().execute(
        "SELECT id FROM alerts LIMIT 1").fetchone()[0]
    am.dismiss(aid)
    assert am.send(_alert()) is True


def test_cross_process_suppression(db):
    """跨进程: 新进程 AlertManager 无内存态, 仍从 DB 派生持续态"""
    am1 = AlertManager(db)
    am1.send(_alert())
    am2 = AlertManager(db)
    assert am2._recent == {}
    assert am2.should_alert(_alert()) is False


def test_different_title_not_suppressed(db):
    """去重按 title 精确匹配 — 不同告警互不影响"""
    am = AlertManager(db)
    am.send(_alert(title="管道失败: ddp"))
    assert am.should_alert(_alert(title="管道失败: factor_update")) is True


def test_db_error_fails_open(db):
    """DB 查询失败 → 放行 (告警链路故障不得静默真实告警)"""
    am = AlertManager(db)
    with patch.object(am.db, "get_connection", side_effect=RuntimeError("db down")):
        assert am._has_open_alert("任意") is False
