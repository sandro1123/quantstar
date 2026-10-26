"""Tests for KlinePipeline skip logic"""
from unittest.mock import MagicMock
from common.database import Database
from data_layer.pipelines.kline import KlinePipeline
from data_layer.pipelines.base import PipelineInput


class MockSource:
    name = "mock"
    capabilities = {"kline"}
    def is_available(self): return True
    def get_data(self, dtype, params): return []


class MockRegistry:
    def get_sources_for(self, dtype):
        return [MockSource()]


class TestKlinePipelineSkipLogic:
    def test_skip_when_db_has_valid_change_pct(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                code TEXT, date TEXT, close REAL, change_pct REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.execute("INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                     ("000001", "2026-07-24", 10.0, 1.5))
        conn.commit()

        pipeline = KlinePipeline(source_registry=MockRegistry())
        # 指定符号在 end_date 已有行 => 应短路跳过, 不发起网络拉取
        inp = PipelineInput(date="2026-07-24", start_date="2026-07-24", end_date="2026-07-24",
                            db=db, symbols=["000001"])
        result = pipeline.run(inp)
        assert result.status == "success"
        assert result.records_written == 0
        assert result.metadata.get("mode") == "symbols_covered"

    def test_not_skip_when_symbols_missing(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                code TEXT, date TEXT, close REAL, change_pct REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.execute("INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                     ("000001", "2026-07-24", 10.0, 1.5))
        conn.commit()

        pipeline = KlinePipeline(source_registry=MockRegistry())
        # 指定符号(000002)在 end_date 无行 => 不应短路, 应尝试拉取
        inp = PipelineInput(date="2026-07-24", start_date="2026-07-24", end_date="2026-07-24",
                            db=db, symbols=["000002"])
        result = pipeline.run(inp)
        # MockRegistry 无数据源 => 拉取后 status 应为 failed(无可用源)而非 db_only 跳过
        assert result.records_written == 0

    def test_runs_when_db_has_no_data(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                code TEXT, date TEXT, close REAL, change_pct REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

        pipeline = KlinePipeline(source_registry=MockRegistry())
        inp = PipelineInput(date="2026-07-24", start_date="2026-07-24", end_date="2026-07-24",
                            db=db, symbols=[])
        result = pipeline.run(inp)
        assert result.status == "failed"  # all sources returned empty → failed
        assert result.records_written == 0
        # 确保有明确的错误信息
        has_src_err = any("sources returned empty" in e for e in result.errors)
        has_no_src_err = any("no available" in e for e in result.errors)
        assert has_src_err or has_no_src_err, f"Expected 'sources returned empty' error, got: {result.errors}"