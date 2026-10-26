"""DL-33: 三个失联管线接线 — index_daily 并入采集 / quality_check+freshness_audit 独立管道。

D-33 定案:
- index_daily 是采集类 (与 shortterm_collect 同性质) → 并入 daily_data_pipeline 的
  sub_pipelines; 未注册则跳过+告警, 失败 best-effort 降级不阻塞主流程。
- quality_check / freshness_audit 是"检查今天数据齐不齐"的校验类 → 建独立管道,
  排在 daily_data_pipeline 之后 (塞进同一管道会检查到半成品然后误报)。

红-绿: 撤回 orchestrator/ops.yaml 改动时本文件必须失败。
"""
from types import SimpleNamespace

import pytest

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
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    # 放一行当日 kline 让 kline 覆盖率探针不报错
    conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-08-22', 10.0)")
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


def _fake_app(db, registry, run_pipeline):
    data = SimpleNamespace(db=db, run_pipeline=run_pipeline, pipelines=registry)
    return SimpleNamespace(data=data, execution=None)


# --- index_daily 并入 daily_data_pipeline ---

def test_index_daily_runs_when_registered(monkeypatch):
    db, orch = _orchestrator()
    called = []

    def run_pipeline(name, pip_input):
        called.append(name)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=2)

    # 注册全部 sub_pipelines 需要的名字 + index_daily
    names = {"stock_list", "kline", "fuyao_financial", "valuation", "moneyflow",
             "concept_daily", "lhb_daily", "market_sentiment", "analyst_forecasts",
             "stock_news", "stock_limit_up_down", "shortterm_collect", "index_daily"}
    app = _fake_app(db, _FakeRegistry(names), run_pipeline)
    monkeypatch.setattr(orch, "_get_app", lambda: app)
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert "index_daily" in called, "index_daily 应被 daily_data_pipeline 调用"


def test_index_daily_skipped_when_unregistered(monkeypatch):
    db, orch = _orchestrator()
    called = []

    def run_pipeline(name, pip_input):
        called.append(name)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    names = {"stock_list", "kline", "fuyao_financial", "valuation", "moneyflow",
             "concept_daily", "lhb_daily", "market_sentiment", "analyst_forecasts",
             "stock_news", "stock_limit_up_down", "shortterm_collect"}  # 无 index_daily
    app = _fake_app(db, _FakeRegistry(names), run_pipeline)
    monkeypatch.setattr(orch, "_get_app", lambda: app)
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert "index_daily" not in called, "未注册时不应调用 index_daily"
    # 未注册只告警不阻塞 — 主流程不应因 index_daily 缺失而失败
    assert not any("index_daily" in str(e) for e in result.errors)


# --- quality_check / freshness_audit 独立管道 ---

def test_quality_check_handler_runs_pipeline(monkeypatch):
    db, orch = _orchestrator()
    called = []

    def run_pipeline(name, pip_input):
        called.append(name)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=3)

    app = _fake_app(db, _FakeRegistry({"quality_check"}), run_pipeline)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_quality_check({})

    assert called == ["quality_check"]
    assert result.success is True
    assert result.metadata.get("quality_records") == 3


def test_freshness_audit_handler_runs_pipeline(monkeypatch):
    db, orch = _orchestrator()
    called = []

    def run_pipeline(name, pip_input):
        called.append(name)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=12)

    app = _fake_app(db, _FakeRegistry({"freshness_audit"}), run_pipeline)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_freshness_audit({})

    assert called == ["freshness_audit"]
    assert result.success is True
    assert result.metadata.get("freshness_records") == 12


def test_quality_check_unregistered_skips_not_fails(monkeypatch):
    db, orch = _orchestrator()
    app = _fake_app(db, _FakeRegistry(set()), lambda n, i: None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_quality_check({})

    # 未注册: 跳过+告警, 不判失败 (校验类缺失不阻塞)
    assert result.success is True
    assert result.metadata.get("skipped") is True


def test_ops_yaml_defines_quality_and_freshness_after_daily():
    """ops.yaml 必须有 quality_check/freshness_audit 定义且 depends_on daily_data_pipeline。"""
    from config_layer import Config
    c = Config()
    c.load()
    pipelines = c.get_section("ops.pipelines")
    for name in ("quality_check", "freshness_audit"):
        assert name in pipelines, f"ops.yaml 缺 {name} 定义"
        assert pipelines[name].get("depends_on") == ["daily_data_pipeline"], \
            f"{name} 应排在 daily_data_pipeline 之后"
