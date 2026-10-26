"""显式源日期冲突不得占据请求日主键；无日期源仍遵循请求参数契约。"""
import sqlite3
from types import SimpleNamespace

import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.lhb import LHBPipeline


@pytest.mark.parametrize("field,value,expected", [
    ("trade_date", "2026-09-16", "failed"),
    ("date", "2026-09-16", "failed"),
    ("trade_date", "2026-09-17", "success"),
])
def test_source_business_date_before_write(monkeypatch, field, value, expected):
    conn = sqlite3.connect(":memory:")
    db = SimpleNamespace(get_connection=lambda: conn)
    pipe = LHBPipeline(db=db)
    row = {"code": "600001", "buy_in": 12, "turnover": 20, "capitalization": 8,
           field: value}
    monkeypatch.setattr(pipe, "_fetch", lambda date: ([row], "test", [], ""))
    try:
        result = pipe.run(PipelineInput(date="2026-09-17", db=db))
        assert result.status == expected
        saved = conn.execute("SELECT date, net_buy_amount FROM lhb_daily").fetchall()
        if expected == "failed":
            assert saved == []
            assert result.records_written == 0
            assert any("2026-09-17" in error and "2026-09-16" in error for error in result.errors)
        else:
            assert saved == [("2026-09-17", 12)]
    finally:
        conn.close()
