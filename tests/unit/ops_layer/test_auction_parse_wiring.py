"""DL-22: auction_parse ops 接线 — auction_collect 冷采集调用

模式 (参照 DL-07 shortterm_collect 接线): 未注册则跳过+告警,
采集失败/超时降级为 warning, 不阻塞 auction_parse 主路径。
"""

from types import SimpleNamespace

from common.database import Database
from data_layer.pipelines.base import PipelineResult
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


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


def _fake_app(db, registry, run_pipeline):
    data_layer = SimpleNamespace(db=db, run_pipeline=run_pipeline, pipelines=registry)
    return SimpleNamespace(data=data_layer, execution=None)


def test_auction_parse_calls_auction_collect_when_registered(monkeypatch):
    db, orchestrator = _orchestrator()
    calls = []

    def run_pipeline(name, pipeline_input):
        calls.append((name, pipeline_input.date))
        return PipelineResult(name=name, date=pipeline_input.date,
                              status="success", records_written=42)

    app = _fake_app(db, _FakeRegistry({"auction_collect"}), run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_auction_parse({})

    assert result.success is True
    assert calls and calls[0][0] == "auction_collect"
    assert result.metadata["auction_records"] == 42
    db.close()


def test_auction_parse_skips_when_pipeline_unregistered(monkeypatch):
    db, orchestrator = _orchestrator()
    calls = []

    def run_pipeline(name, pipeline_input):
        calls.append(name)
        return None

    app = _fake_app(db, _FakeRegistry(set()), run_pipeline)  # 未注册
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_auction_parse({})

    assert result.success is True  # 跳过不阻塞
    assert calls == []  # 未调用
    assert result.metadata["auction_records"] is None
    db.close()


def test_auction_parse_degrades_on_collect_failure(monkeypatch):
    db, orchestrator = _orchestrator()

    def run_pipeline(name, pipeline_input):
        raise RuntimeError("fuyao auction endpoint down")

    app = _fake_app(db, _FakeRegistry({"auction_collect"}), run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_auction_parse({})

    assert result.success is True  # best-effort 降级不阻塞
    assert result.metadata["auction_records"] is None
    db.close()


def test_auction_parse_handles_timeout_none_result(monkeypatch):
    db, orchestrator = _orchestrator()

    def run_pipeline(name, pipeline_input):
        return None  # with_timeout 超时返回 None

    app = _fake_app(db, _FakeRegistry({"auction_collect"}), run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_auction_parse({})

    assert result.success is True
    assert result.metadata["auction_records"] == 0
    db.close()


def test_auction_parse_survives_data_layer_without_pipelines_attr(monkeypatch):
    db, orchestrator = _orchestrator()
    # 旧版 data layer / 测试 fake 可能无 pipelines 属性 — getattr 兜底
    app = SimpleNamespace(
        data=SimpleNamespace(db=db, run_pipeline=lambda n, i: None),
        execution=None)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_auction_parse({})

    assert result.success is True
    assert result.metadata["auction_records"] is None
    db.close()
