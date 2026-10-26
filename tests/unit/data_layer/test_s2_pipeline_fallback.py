"""S2 F2.2 — 四管道多源 fallback 回归测试 (FakeRegistry)"""

import pytest

from common.database import Database
from data_layer.pipelines.analyst_forecast import AnalystForecastPipeline
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.concept import ConceptPipeline
from data_layer.pipelines.lhb import LHBPipeline
from data_layer.pipelines.sentiment import SentimentPipeline


class FakeSource:
    def __init__(self, name, data_by_type, capabilities=None, available=True):
        self.name = name
        self._data = data_by_type
        self.capabilities = capabilities or set(data_by_type.keys())
        self._available = available
        self.calls = []

    def is_available(self):
        return self._available

    def get_data(self, data_type, params):
        self.calls.append((data_type, dict(params)))
        return self._data.get(data_type)


class FakeRegistry:
    def __init__(self, sources):
        self.sources = sources

    def get_sources_for(self, data_type):
        return [s for s in self.sources if data_type in s.capabilities and s.is_available()]


class FailingSource(FakeSource):
    def get_data(self, data_type, params):
        self.calls.append((data_type, dict(params)))
        raise RuntimeError(f"{self.name} down")


def _db():
    db = Database(":memory:")
    db.init_db()
    db.get_connection().execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            industry TEXT DEFAULT '', market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '', is_active INTEGER DEFAULT 1
        )
    """)
    return db


class TestSentimentFallback:
    def test_first_source_wins(self):
        s1 = FakeSource("s1", {"sentiment": {"info": {"sign": "55", "ZT": "3", "DT": "1"}}})
        s2 = FakeSource("s2", {"sentiment": {"info": {"sign": "99"}}})
        db = _db()
        pipe = SentimentPipeline(db=db, source_registry=FakeRegistry([s1, s2]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        assert result.records_written == 1
        assert not s2.calls
        row = db.get_connection().execute("SELECT market_temp, zt_count FROM market_sentiment").fetchone()
        assert row[0] == 55.0
        assert row[1] == 3

    def test_fallback_to_second_source(self):
        s1 = FailingSource("s1", {"sentiment": None})
        s2 = FakeSource("s2", {"sentiment": {"info": {"sign": "60", "ZT": "2"}}})
        db = _db()
        pipe = SentimentPipeline(db=db, source_registry=FakeRegistry([s1, s2]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        assert s2.calls

    def test_all_sources_fail_partial(self):
        s1 = FailingSource("s1", {"sentiment": None})
        db = _db()
        pipe = SentimentPipeline(db=db, source_registry=FakeRegistry([s1]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.records_written == 0


class TestLHBFallback:
    def test_fallback_order_and_write(self):
        s1 = FailingSource("s1", {"lhb": None})
        s2 = FakeSource("s2", {"lhb": [{"stock_code": "600000", "buy_in": 1234.5, "turnover": 100, "capitalization": 50}]})
        db = _db()
        db.get_connection().commit()
        pipe = LHBPipeline(db=db, source_registry=FakeRegistry([s1, s2]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        assert result.records_written == 1
        row = db.get_connection().execute("SELECT net_buy_amount FROM lhb_daily WHERE code='600000'").fetchone()
        assert row[0] == pytest.approx(1234.5)


class TestConceptFallback:
    def test_concept_vs_industry_fallback_source_tag(self):
        limit_data = [{"plate_name": "机器人", "stocks": [{"stock_code": "600000"}]}]
        s1 = FakeSource("s1", {"limit_up": limit_data, "stock_basic": [{"symbol": "000001", "industry": "银行"}]})
        db = _db()
        pipe = ConceptPipeline(db=db, source_registry=FakeRegistry([s1]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        conn = db.get_connection()
        concept_src = conn.execute("SELECT source FROM concept_daily WHERE concept_name='机器人'").fetchone()[0]
        industry_src = conn.execute("SELECT source FROM concept_daily WHERE concept_name='银行'").fetchone()[0]
        assert concept_src == "concept"
        assert industry_src == "industry_fallback"

    def test_fallback_to_second_source(self):
        s1 = FailingSource("s1", {"limit_up": None, "stock_basic": None})
        s2 = FakeSource("s2", {"limit_up": [{"plate_name": "AI", "stocks": [{"stock_code": "300001"}]}], "stock_basic": []})
        db = _db()
        pipe = ConceptPipeline(db=db, source_registry=FakeRegistry([s1, s2]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        row = db.get_connection().execute("SELECT concept_name FROM concept_daily WHERE code='300001'").fetchone()
        assert row[0] == "AI"


class TestAnalystFallback:
    def _db_with_codes(self, codes):
        db = _db()
        conn = db.get_connection()
        conn.executemany("INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
                         [(c, f"n{c}") for c in codes])
        conn.commit()
        return db

    def test_no_200_truncation_full_market(self):
        codes = [f"{600000 + i}" for i in range(350)]
        db = self._db_with_codes(codes)
        hits = {c: {"consensus_score": 1.0, "target_price": 10.0, "rating": "买入"} for c in codes[:10]}

        class BulkFakeSource(FakeSource):
            def get_data(self, data_type, params):
                self.calls.append((data_type, dict(params)))
                return hits.get(params.get("code"))

        s = BulkFakeSource("s1", {"analyst_forecast": None}, capabilities={"analyst_forecast"})
        pipe = AnalystForecastPipeline(db=db, source_registry=FakeRegistry([s]))
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.status == "success"
        assert result.records_written == 10
        assert result.metadata["attempted"] == 350
        assert result.metadata["attempted"] > 200

    def test_max_codes_protective_upper_bound(self):
        codes = [f"{600000 + i}" for i in range(50)]
        db = self._db_with_codes(codes)
        s = FakeSource("s1", {"analyst_forecast": {"consensus_score": 1.0, "target_price": 10.0, "rating": "买入"}})
        pipe = AnalystForecastPipeline(db=db, source_registry=FakeRegistry([s]), max_codes=5)
        result = pipe.run(PipelineInput(date="2026-08-16", db=db))
        assert result.metadata["attempted"] == 5
        assert result.records_written == 5
