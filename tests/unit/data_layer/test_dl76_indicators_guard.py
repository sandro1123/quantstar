"""DL-76: fuyao indicators 为空时不写 Financial 记录 (防 roe=0 脏行) + 留痕计数

背景 (DL-76 实测): ded6595 后 get_financial_indicators 传 report=best_report,
当该季度 indicators 未发布时返回空 abilities → _extract_indicator 全 0.0 →
旧代码会写入 roe=0/gm=0/nm=0 的 Financial 记录 (把"不知道"写成 0.0)。

修法: indicators 为空 (None 或 abilities 空) 时跳过写入, 计数进
metadata["indicators_missing"], 全缺时降级 partial + warning (铁律 #12 留痕)。

本测试:
- 正例: indicators 正常 → 写入记录, indicators_missing=0
- 负向对照①: indicators 返回 None → 不写记录, indicators_missing 计数, 无 roe=0 行
- 负向对照②: indicators 返回空 abilities → 同上
- 负向对照③: 全部股票 indicators 缺失 → status 降级 partial (非 success)
"""
import pytest
from common.database import Database
from data_layer.pipelines.fuyao import FuyaoPipeline
from data_layer.pipelines.base import PipelineInput
from data_layer.repos import get_repo


class _FakeClient:
    """模拟 FuyaoClient; indicators_mode 控制 get_financial_indicators 返回。"""

    def __init__(self, indicators_mode="ok"):
        self.api_key = "test-key"
        self._indicators_mode = indicators_mode  # ok / none / empty_abilities
        self._income_rows = [{
            "basic_eps": 0.67,
            "operating_income": 35277000000.0,
            "parent_holder_net_profit": 14523000000.0,
            "period_end_ms": 1774886400000,  # 2026-03-30
        }]

    def available(self) -> bool:
        return True

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
        if self._indicators_mode == "none":
            return None
        if self._indicators_mode == "empty_abilities":
            return {"abilities": []}
        return {"abilities": [{"indicators": [
            {"index_id": "index_weighted_avg_roe", "value": 10.0},
            {"index_id": "sale_gross_margin", "value": 89.76},
            {"index_id": "sale_net_interest_ratio", "value": 52.22},
        ]}]}


class TestDL76IndicatorsGuard:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.db = Database(":memory:")
        self.repo = get_repo(self.db)
        self.repo.stock.batch_upsert([
            {"code": "000001", "name": "平安银行", "is_active": 1},
            {"code": "000002", "name": "万科A", "is_active": 1},
        ])

    def _pipeline(self, indicators_mode="ok"):
        p = FuyaoPipeline()
        p._client = _FakeClient(indicators_mode=indicators_mode)
        return p

    def _financial_rows(self):
        conn = self.db.get_connection()
        return conn.execute("SELECT code, roe, gross_margin FROM stock_financials").fetchall()

    def test_indicators_ok_writes_record(self):
        """正例: indicators 正常 → 写入, indicators_missing=0"""
        result = self._pipeline("ok").run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "success"
        assert result.records_written == 2
        assert result.metadata.get("indicators_missing") == 0
        rows = self._financial_rows()
        assert len(rows) == 2
        # roe 应为真实值 10.0, 不是 0
        for r in rows:
            assert r["roe"] == 10.0
            assert r["gross_margin"] == 89.76

    def test_indicators_none_skips_write_and_counts(self):
        """负向对照①: indicators=None → 不写记录, 计数, 无 roe=0 行"""
        result = self._pipeline("none").run(PipelineInput(date="2026-08-06", db=self.db))
        # 全部 2 只都因 indicators 缺失被跳过 → total=0, indicators_missing=2
        assert result.metadata.get("indicators_missing") == 2
        assert result.records_written == 0
        # 关键: 没有写入任何 roe=0 的脏行
        assert len(self._financial_rows()) == 0
        # 留痕: warning 提及跳过
        assert any("indicators 未发布" in w for w in result.warnings)

    def test_indicators_empty_abilities_skips_write(self):
        """负向对照②: abilities=[] → 同样不写记录"""
        result = self._pipeline("empty_abilities").run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.metadata.get("indicators_missing") == 2
        assert result.records_written == 0
        assert len(self._financial_rows()) == 0

    def test_all_indicators_missing_degrades_to_partial(self):
        """负向对照③: 全缺 → status 降级 partial (非 success), 防假成功"""
        result = self._pipeline("none").run(PipelineInput(date="2026-08-06", db=self.db))
        assert result.status == "partial"
        assert result.records_written == 0
