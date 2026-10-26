"""DL-23: 指数日线管线 + 读 API — :memory: 库, 无生产写

验证: 建表/幂等写入/部分失败降级/读 API 区间过滤/表缺失返回空
"""

import sqlite3
from unittest.mock import patch as mock_patch

import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.index_daily import IndexDailyPipeline


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _rows(db, code="000300.SH"):
    return [{"date": "2026-08-14", "open": 4672.0, "high": 4676.0,
             "low": 4637.0, "close": 4665.0, "volume": 1.7e10},
            {"date": "2026-08-17", "open": 4672.0, "high": 4742.0,
             "low": 4662.0, "close": 4741.0, "volume": 1.9e10}]


def test_pipeline_creates_table_and_writes():
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    with mock_patch.object(pipe, "_fetch", side_effect=lambda s: _rows(db)):
        result = pipe.run(PipelineInput(date="2026-08-17", db=db))
    assert result.status == "success"
    assert result.records_written == 4  # 2 指数 x 2 行
    n = db.conn.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert n == 4


def test_pipeline_idempotent():
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    with mock_patch.object(pipe, "_fetch", side_effect=lambda s: _rows(db)):
        pipe.run(PipelineInput(date="2026-08-17", db=db))
        pipe.run(PipelineInput(date="2026-08-17", db=db))
    n = db.conn.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
    assert n == 4  # INSERT OR REPLACE 幂等


def test_pipeline_partial_when_one_source_fails():
    db = _FakeDB()
    pipe = IndexDailyPipeline()

    def _fetch(sina_symbol):
        if sina_symbol == "sh000300":
            return _rows(db)
        raise ConnectionError("em refused")

    with mock_patch.object(pipe, "_fetch", side_effect=_fetch):
        result = pipe.run(PipelineInput(date="2026-08-17", db=db))
    assert result.status == "partial"
    assert result.records_written == 2
    assert len(result.errors) == 1


def test_pipeline_partial_when_all_empty():
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    with mock_patch.object(pipe, "_fetch", return_value=[]):
        result = pipe.run(PipelineInput(date="2026-08-17", db=db))
    assert result.status == "failed"
    assert result.records_written == 0


def _stale_only_rows():
    """源只回补到 09-16 (缺 09-17 当日行) 的固定夹具。"""
    return [{"date": "2026-09-16", "open": 4700.0, "high": 4710.0,
             "low": 4690.0, "close": 4705.0, "volume": 1.8e10}]


def test_pipeline_stale_only_data_not_success_on_business_date():
    """index_daily 业务日判据 (2026-09-17 固定夹具):

    源全历史返回只到 09-16 (当日行未发布) → 输入业务日 09-17 必须
    partial + 明确缺口留痕, 不得 success; 禁 stale-date 换标
    (09-16 的行不得被写进 09-17, 也不得拿历史回填量冒充当日成功)。
    病根: 现行 run() 只看 total>0 (历史回填行) 就报 success,
    结构性 T-1 数据 (AUD-002 09-16 定谳) 被吞成假绿。
    """
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    with mock_patch.object(pipe, "_fetch", return_value=_stale_only_rows()):
        result = pipe.run(PipelineInput(date="2026-09-17", db=db))
    assert result.status == "partial"
    assert any("2026-09-17" in e for e in result.errors), result.errors
    # stale-date 换标禁令: 表里 09-17 当日必须 0 行
    n = db.conn.execute(
        "SELECT COUNT(*) FROM index_daily WHERE code='000300.SH' AND date='2026-09-17'"
    ).fetchone()[0]
    assert n == 0


def test_pipeline_success_only_when_business_date_row_present():
    """源返回含 09-17 当日行 → success, 且当日行真实落库。"""
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    rows = _stale_only_rows() + [
        {"date": "2026-09-17", "open": 4712.0, "high": 4730.0,
         "low": 4701.0, "close": 4725.0, "volume": 1.9e10}]
    with mock_patch.object(pipe, "_fetch", return_value=rows):
        result = pipe.run(PipelineInput(date="2026-09-17", db=db))
    assert result.status == "success"
    n = db.conn.execute(
        "SELECT COUNT(*) FROM index_daily WHERE code='000300.SH' AND date='2026-09-17'"
    ).fetchone()[0]
    assert n == 1


def test_read_api_range_filter():
    db = _FakeDB()
    pipe = IndexDailyPipeline()
    with mock_patch.object(pipe, "_fetch", side_effect=lambda s: _rows(db)):
        pipe.run(PipelineInput(date="2026-08-17", db=db))
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    dl.db = db
    rows = dl.get_index_daily("000300.SH", start_date="2026-08-15")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-17"
    assert rows[0]["close"] == 4741.0


def test_read_api_missing_table_returns_empty():
    db = _FakeDB()
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    dl.db = db
    assert dl.get_index_daily("000300.SH") == []
