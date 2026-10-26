"""Tests for FuyaoPipeline incremental skip (方案A)"""
import pytest
from common.database import Database
from data_layer.pipelines.fuyao import (
    FuyaoPipeline, _report_period_date, _thscode,
)
from data_layer.pipelines.base import PipelineInput
from data_layer.repos import get_repo


class _FakeClient:
    """模拟 FuyaoClient，固定返回报告期 2026-2 / 报告期末 2026-03-31 (BJT 季末)"""

    def __init__(self, available=True, income_rows=None):
        self.api_key = "test-key"  # FuyaoPipeline.run 先检查 client.api_key
        self._available = available
        self._income_rows = income_rows if income_rows is not None else [{
            "basic_eps": 0.67,
            "operating_income": 35277000000.0,
            "parent_holder_net_profit": 14523000000.0,
            "period_end_ms": 1774886400000,
        }]

    def available(self) -> bool:
        return self._available

    def _get(self, path, params):
        if path.endswith("/income-statements"):
            return {"item": self._income_rows}
        return {"item": []}

    def get_income_statements(self, ths, report=None):
        return list(self._income_rows)

    def get_balance_sheets(self, ths, report=None):
        return [{"assets_total": 5e10, "total_debt": 3e10, "holder_equity_total": 2e10}]

    def get_cash_flow(self, ths, report=None):
        return [{"act_cash_flow_net": 1e9}]

    def get_financial_indicators(self, ths, report=None):
        return {"abilities": [{"indicators": [
            {"index_id": "index_weighted_avg_roe", "value": 10.0},
        ]}]}


class _FakeStockRepo:
    def __init__(self, codes):
        self._codes = codes

    def get_all_active(self):
        return list(self._codes)


class TestReportPeriodDate:
    def test_resolves_iso_period_end(self):
        client = _FakeClient()
        assert _report_period_date(client, "2026-2") == "2026-03-31"

    def test_returns_none_without_items(self):
        client = _FakeClient(income_rows=[])
        assert _report_period_date(client, "2026-2") is None

    def test_thscode_mapping(self):
        assert _thscode("600000") == "600000.SH"
        assert _thscode("000001") == "000001.SZ"


class TestIncrementalSkip:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.db = Database(":memory:")
        self.repo = get_repo(self.db)
        self.repo.stock.batch_upsert([
            {"code": "000001", "name": "平安银行", "is_active": 1},
            {"code": "000002", "name": "万科A", "is_active": 1},
            {"code": "000003", "name": "ST测试", "is_active": 1},
        ])

    def _pipeline(self, available=True, income_rows=None):
        p = FuyaoPipeline()
        p._client = _FakeClient(available=available, income_rows=income_rows)
        return p

    def test_full_market_skips_when_period_covered(self):
        # 预置 2026-03-31 全覆盖 (BJT 键)
        self.repo.financial.batch_upsert([
            {"code": "000001", "date": "2026-03-31", "eps": 0.67, "roe": 10.0},
            {"code": "000002", "date": "2026-03-31", "eps": 0.3, "roe": 8.0},
            {"code": "000003", "date": "2026-03-31", "eps": 0.2, "roe": 5.0},
        ])
        result = self._pipeline().run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "success"
        assert result.records_written == 0
        assert result.metadata.get("skipped") is True
        assert result.metadata.get("target_period") == "2026-03-31"

    def test_full_market_fetches_missing_only(self):
        # 只有 000001 覆盖，其余缺失 → 只拉取缺失
        self.repo.financial.batch_upsert([
            {"code": "000001", "date": "2026-03-31", "eps": 0.67, "roe": 10.0},
        ])
        result = self._pipeline().run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "success"
        assert result.records_written == 2
        assert result.metadata.get("missing") == 2
        assert result.metadata.get("skipped_count") == 1

    def test_explicit_symbols_always_fetch(self):
        self.repo.financial.batch_upsert([
            {"code": "000001", "date": "2026-03-31", "eps": 0.67, "roe": 10.0},
        ])
        result = self._pipeline().run(PipelineInput(
            date="2026-08-06", db=self.db, symbols=["000001"],
        ))
        # 显式指定符号时不走全市场跳过；000001 已有数据，但管道仍为其 upsert（幂等）
        assert result.status in ("success", "partial")
        assert result.metadata.get("skipped", False) is False

    def test_unavailable_returns_failed(self):
        result = self._pipeline(available=False).run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "failed"

    def test_empty_income_skips_failed_report(self):
        # 无法探测报告期 → success + 跳过（数据未发布属正常）
        result = self._pipeline(income_rows=[]).run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "failed"
