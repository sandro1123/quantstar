"""B9 刀1 (fix/morning-brief-red): 晨报 build 返 None 必须是失败，不能假 success。

背景: 10-09 起晨报停推 12 交易日，管道天天 success (dur 60/61 =
with_timeout(60) 截断指纹)，msg is None 被静默吞掉。
修法: _run_morning_brief 里 msg is None → success=False + errors 含
"not delivered"，delivered 进 success 判据。
"""
from types import SimpleNamespace

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def _fake_app(build_result, sent):
    def fake_send(msg, report_mode="debug"):
        sent.append((msg, report_mode))

    report = SimpleNamespace(
        build_morning_brief=lambda date, report_mode="debug": build_result)
    delivery = SimpleNamespace(send=fake_send)
    return SimpleNamespace(report=report, delivery=delivery)


class TestMorningBriefNoneIsFailure:
    def test_none_build_returns_failure_with_not_delivered(self, monkeypatch):
        db, orch = _orchestrator()
        sent = []
        monkeypatch.setattr(
            "bootstrap.bootstrap_app", lambda: _fake_app(None, sent))
        monkeypatch.setattr(
            "ops_layer.orchestrator.cfg_get", lambda key, default=None: "debug")
        result = orch._run_morning_brief({})
        assert result.success is False
        assert any("not delivered" in e for e in result.errors)
        assert result.metadata["delivered"] is False
        assert sent == []
        db.close()

    def test_ok_build_returns_success_and_sends(self, monkeypatch):
        db, orch = _orchestrator()
        msg = SimpleNamespace(content_type="morning_brief")
        sent = []
        monkeypatch.setattr(
            "bootstrap.bootstrap_app", lambda: _fake_app(msg, sent))
        monkeypatch.setattr(
            "ops_layer.orchestrator.cfg_get", lambda key, default=None: "debug")
        result = orch._run_morning_brief({})
        assert result.success is True
        assert result.metadata["delivered"] is True
        assert [m for m, _ in sent] == [msg]
        db.close()
