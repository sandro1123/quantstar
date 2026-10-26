"""P0-5: source_crosscheck 源健康第三方对拍 (新浪参照)。

:memory: 库, 无生产写; 新浪 HTTP 全 monkeypatch, 不发真实请求。
红-绿: 撤回 pipelines/source_crosscheck.py + orchestrator handler 时本文件必须失败。
"""
from types import SimpleNamespace

import pytest

from common.database import Database
from data_layer.pipelines.base import PipelineInput, PipelineResult
from data_layer.pipelines.source_crosscheck import (
    SourceCrosscheckPipeline,
    _parse_sina_response,
    _to_sina_symbol,
)
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

SINA_SAMPLE = (
    'var hq_str_sh600519="贵州茅台,1304.0,1302.8,1292.3,1305.0,1288.0,1292.3,'
    '1292.36,2476747,3203715661.0,7025,1292.3,200,1292.29,2026-08-27,15:00:00,00";\n'
    'var hq_str_sz000001="平安银行,11.7,11.73,11.59,11.72,11.51,11.59,11.6,'
    '97570170,1131000125.95,63100,11.59,2026-08-27,15:00:00,00";\n'
    'var hq_str_sh601318="";\n'  # 停牌 → 空串, 不计差异
)


def _db_with_klines(rows):
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, volume REAL)")
    for code, date, close, volume in rows:
        conn.execute("INSERT INTO daily_klines VALUES (?, ?, ?, ?)",
                     (code, date, close, volume))
    conn.commit()
    return db


class _FakeResp:
    def __init__(self, text):
        self.content = text.encode("gbk")

    def raise_for_status(self):
        pass


# --- 解析/映射单元 ---

def test_to_sina_symbol_prefix_and_bj_excluded():
    assert _to_sina_symbol("600519") == "sh600519"
    assert _to_sina_symbol("000001") == "sz000001"
    assert _to_sina_symbol("300750") == "sz300750"
    assert _to_sina_symbol("830799") is None  # 北交所新浪不覆盖
    assert _to_sina_symbol("430047") is None


def test_parse_sina_response_fields_and_empty_skipped():
    ref = _parse_sina_response(SINA_SAMPLE)
    assert set(ref) == {"600519", "000001"}  # 停牌空串被跳过
    assert ref["600519"]["close"] == pytest.approx(1292.3)
    assert ref["600519"]["volume"] == pytest.approx(2476747)
    assert ref["000001"]["close"] == pytest.approx(11.59)


# --- 管道行为 ---

def test_crosscheck_all_match_no_diff(monkeypatch):
    db = _db_with_klines([
        ("600519", "2026-08-27", 1292.3, 2476747),
        ("000001", "2026-08-27", 11.59, 97570170),
    ])
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(SINA_SAMPLE))
    pipe = SourceCrosscheckPipeline(db=db)
    result = pipe.execute(PipelineInput(date="2026-08-27", db=db,
                                        config={"sample_size": 50}))
    assert result.status == "success"
    assert result.metadata["n_diff"] == 0
    n = db.get_connection().execute(
        "SELECT count(*) FROM source_crosscheck_diffs").fetchone()[0]
    assert n == 0


def test_crosscheck_detects_close_and_volume_diff(monkeypatch):
    db = _db_with_klines([
        # close 差 0.5 (>0.011 容差) + volume 差 10% (>1% 容差) → 2 条差异
        ("600519", "2026-08-27", 1292.8, 2700000),
    ])
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(SINA_SAMPLE))
    pipe = SourceCrosscheckPipeline(db=db)
    result = pipe.execute(PipelineInput(date="2026-08-27", db=db))
    assert result.status == "success"
    assert result.metadata["n_diff"] == 2
    rows = db.get_connection().execute(
        "SELECT field, main_value, ref_value FROM source_crosscheck_diffs "
        "ORDER BY field").fetchall()
    assert {r["field"] for r in rows} == {"close", "volume"}


def test_crosscheck_suspended_not_counted_as_diff(monkeypatch):
    # 601318 新浪返回空串 (停牌) → missing, 不算差异
    db = _db_with_klines([("601318", "2026-08-27", 55.87, 64656841)])
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(SINA_SAMPLE))
    pipe = SourceCrosscheckPipeline(db=db)
    result = pipe.execute(PipelineInput(date="2026-08-27", db=db))
    assert result.status == "success"
    assert result.metadata["n_diff"] == 0
    assert result.metadata["n_compared"] == 0


def test_crosscheck_ref_unavailable_partial(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("sina down")
    monkeypatch.setattr("requests.get", _boom)
    db = _db_with_klines([("600519", "2026-08-27", 1292.3, 2476747)])
    pipe = SourceCrosscheckPipeline(db=db)
    result = pipe.execute(PipelineInput(date="2026-08-27", db=db))
    # 参照源不可达 → partial 不阻塞 (观测件), 铁律#12 留痕
    assert result.status == "partial"
    assert result.metadata.get("ref_unavailable") is True


def test_crosscheck_no_candidates_skips_cleanly():
    db = _db_with_klines([])  # 当日无行
    pipe = SourceCrosscheckPipeline(db=db)
    result = pipe.execute(PipelineInput(date="2026-08-27", db=db))
    assert result.status == "success"
    assert result.metadata.get("skipped") is True


# --- orchestrator handler ---

def _orchestrator(db):
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    return PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def test_handler_alerts_when_diff_rate_exceeds(monkeypatch):
    db = _db_with_klines([])
    orch = _orchestrator(db)
    sent = []
    monkeypatch.setattr(orch.alert_manager, "send", lambda a: sent.append(a) or True)
    fake = PipelineResult(
        name="source_crosscheck", date="2026-08-27", status="success",
        metadata={"n_sampled": 50, "n_compared": 50, "n_diff": 5,
                  "diff_rate": 0.10, "diff_rate_alert": 0.05,
                  "ref_source": "sina_hq"})
    app = SimpleNamespace(
        data=SimpleNamespace(db=db, run_pipeline=lambda n, i: fake,
                             pipelines={"source_crosscheck": object()}),
        execution=None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_source_crosscheck({})

    assert result.success is True
    assert result.metadata["crosscheck_diffs"] == 5
    assert len(sent) == 1 and sent[0].severity == "warning"


def test_handler_no_alert_when_within_threshold(monkeypatch):
    db = _db_with_klines([])
    orch = _orchestrator(db)
    sent = []
    monkeypatch.setattr(orch.alert_manager, "send", lambda a: sent.append(a) or True)
    fake = PipelineResult(
        name="source_crosscheck", date="2026-08-27", status="success",
        metadata={"n_sampled": 50, "n_compared": 50, "n_diff": 1,
                  "diff_rate": 0.02, "diff_rate_alert": 0.05,
                  "ref_source": "sina_hq"})
    app = SimpleNamespace(
        data=SimpleNamespace(db=db, run_pipeline=lambda n, i: fake,
                             pipelines={"source_crosscheck": object()}),
        execution=None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_source_crosscheck({})

    assert result.success is True
    assert not sent, "阈值内不应告警"


def test_handler_unregistered_skips(monkeypatch):
    db = _db_with_klines([])
    orch = _orchestrator(db)
    app = SimpleNamespace(
        data=SimpleNamespace(db=db, run_pipeline=lambda n, i: None,
                             pipelines={}),
        execution=None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_source_crosscheck({})

    assert result.success is True
    assert result.metadata.get("skipped") is True
