"""DL-35 复权因子三源接入 — 单测

测行为边界 (不复述实现):
- AdjustRepo 幂等 upsert + 按日期取最近生效因子
- AdjustFactorPipeline 当日增量: 无事件=success/有事件写三表/主源挂=failed
- 交叉验证差异记 warnings 不阻塞
- baostock 适配器: 单位不转换 (volume 股/amount 元)、adjustflag='3' 不复权、
  北交所代码拒绝、登录失败降级
- repos/kline 写入 adjust_type='raw' (F-21 标记修正的写侧)
"""
from unittest.mock import MagicMock, patch

import pytest

from common.database import Database
from data_layer.repos.adjust import AdjustRepo


@pytest.fixture
def db(tmp_path):
    # 每测试独立临时库 (test_db_path 是会话级, 跨测试会累积行)
    d = Database(str(tmp_path / "dl35_test.db"))
    d.init_db()
    return d


class TestAdjustRepo:
    def test_upsert_and_read_factor(self, db):
        repo = AdjustRepo(db)
        n = repo.upsert_adjust_factors([
            {"code": "600519", "ex_date": "2026-06-26", "adjust_factor": 7.669, "source": "baostock"},
            {"code": "600519", "ex_date": "2025-12-19", "adjust_factor": 7.5, "source": "baostock"},
        ])
        assert n == 2
        # 目标日期生效因子 = 之前(含)最近 ex_date
        assert repo.get_adjust_factor("600519", "2026-07-01") == pytest.approx(7.669)
        assert repo.get_adjust_factor("600519", "2026-01-01") == pytest.approx(7.5)
        # 早于所有除权日 → None
        assert repo.get_adjust_factor("600519", "2025-01-01") is None
        # 无记录股票 → None
        assert repo.get_adjust_factor("000001", "2026-07-01") is None

    def test_upsert_idempotent(self, db):
        repo = AdjustRepo(db)
        rec = {"code": "600519", "ex_date": "2026-06-26", "adjust_factor": 7.669, "source": "baostock"}
        repo.upsert_adjust_factors([rec])
        repo.upsert_adjust_factors([rec])  # 重复写不增行 (PK code+date)
        assert repo.count("adjust_factors") == 1

    def test_upsert_dividends_and_splits(self, db):
        repo = AdjustRepo(db)
        repo.upsert_dividends([{
            "code": "600519", "ex_date": "2026-06-26", "cash_dividend": 28.02423,
            "stock_dividend": 0.0, "stock_transfer": 0.0, "source": "baostock"}])
        repo.upsert_splits([{
            "code": "000001", "ex_date": "2026-06-12", "split_factor": 1.5, "source": "baostock"}])
        assert repo.count("dividends") == 1
        assert repo.count("splits") == 1

    def test_count_rejects_bad_table(self, db):
        repo = AdjustRepo(db)
        assert repo.count("daily_klines; drop") == 0


def _mock_registry(sources):
    reg = MagicMock()
    reg.get_all_sources.return_value = sources
    return reg


def _mock_baostock(events=None, dividends=None):
    bs = MagicMock()
    bs.name = "baostock"
    bs.is_available.return_value = True
    bs.get_daily_adjust_factors.return_value = events or []
    bs.get_dividends.side_effect = lambda code, year: [
        d for d in (dividends or []) if d["code"] == code]
    return bs


class TestAdjustFactorPipeline:
    def _run(self, db, sources, date="2026-07-29"):
        from data_layer.pipelines.adjust_factor import AdjustFactorPipeline
        from data_layer.pipelines.base import PipelineInput
        pipe = AdjustFactorPipeline(source_registry=_mock_registry(sources))
        return pipe.execute(PipelineInput(date=date, db=db))

    def test_no_events_is_success(self, db):
        bs = _mock_baostock(events=[])
        result = self._run(db, [bs])
        assert result.status == "success"
        assert result.records_written == 0
        assert result.metadata["events"] == 0

    def test_events_write_three_tables(self, db):
        events = [{"code": "600519", "ex_date": "2026-07-29",
                   "back_factor": 7.669, "fore_factor": 1.0}]
        divs = [{"code": "600519", "ex_date": "2026-07-29", "cash_dividend": 28.02,
                 "stock_dividend": 0.0, "stock_transfer": 0.0, "source": "baostock"}]
        bs = _mock_baostock(events=events, dividends=divs)
        result = self._run(db, [bs])
        assert result.status == "success"
        assert result.metadata["factors"] == 1
        assert result.metadata["dividends"] == 1
        repo = AdjustRepo(db)
        assert repo.count("adjust_factors") == 1
        assert repo.count("dividends") == 1

    def test_split_generated_from_stock_transfer(self, db):
        events = [{"code": "000001", "ex_date": "2026-07-29",
                   "back_factor": 2.0, "fore_factor": 1.0}]
        divs = [{"code": "000001", "ex_date": "2026-07-29", "cash_dividend": 0.0,
                 "stock_dividend": 0.3, "stock_transfer": 0.2, "source": "baostock"}]
        bs = _mock_baostock(events=events, dividends=divs)
        result = self._run(db, [bs])
        assert result.metadata["splits"] == 1
        conn = db.get_connection()
        row = conn.execute("SELECT split_factor FROM splits WHERE code='000001'").fetchone()
        assert row["split_factor"] == pytest.approx(1.5)

    def test_baostock_unavailable_fails(self, db):
        result = self._run(db, [])  # 无 baostock
        assert result.status == "failed"
        assert any("baostock" in e for e in result.errors)

    def test_cross_validate_mismatch_warns_not_blocks(self, db):
        events = [{"code": "600519", "ex_date": "2026-07-29",
                   "back_factor": 7.669, "fore_factor": 1.0}]
        divs = [{"code": "600519", "ex_date": "2026-07-29", "cash_dividend": 28.02,
                 "stock_dividend": 0.0, "stock_transfer": 0.0, "source": "baostock"}]
        bs = _mock_baostock(events=events, dividends=divs)
        # mootdx 给出明显不同的分红 (28.02 vs 10.0 元/股)
        mootdx = MagicMock()
        mootdx.name = "mootdx"
        mootdx.is_available.return_value = True
        mootdx.get_xdxr.return_value = {"fenhong": 100.0, "songzhuangu": 0.0}  # 10 元/股
        result = self._run(db, [bs, mootdx])
        assert result.status == "success"  # 差异不阻塞
        assert any("现金分红不一致" in w for w in result.warnings)


class TestBaoStockAdapter:
    def _adapter(self):
        from data_layer.sources.baostock_source import BaoStockSourceAdapter
        return BaoStockSourceAdapter()

    def test_capabilities_dl35_only(self):
        ad = self._adapter()
        # DL-35 先只开放复权元数据; kline_bulk 留给 DL-25
        assert "adjust_factor" in ad.capabilities
        assert "dividend" in ad.capabilities

    def test_rejects_bj_code(self):
        ad = self._adapter()
        assert ad._to_baostock_code("430047") is None  # 北交所
        assert ad._to_baostock_code("830799") is None
        assert ad._to_baostock_code("600519") == "sh.600519"
        assert ad._to_baostock_code("000001") == "sz.000001"
        assert ad._to_baostock_code("300750") == "sz.300750"

    def test_bulk_no_unit_conversion(self):
        """DL-25 前置: baostock volume=股 amount=元, 不得照抄 tushare ×100/×1000"""
        ad = self._adapter()
        fake_result = MagicMock()
        fake_result.error_code = "0"
        fake_result.fields = ["date", "code", "open", "high", "low", "close",
                              "preclose", "volume", "amount", "adjustflag", "turn",
                              "tradestatus", "pctChg", "peTTM", "pbMRQ", "psTTM",
                              "pcfNcfTTM", "isST"]
        rows = [["2026-08-21", "sh.600000", "9.09", "9.15", "9.03", "9.05",
                 "9.11", "51270289", "465159863.32", "3", "0.15", "1",
                 "-0.66", "5.88", "0.40", "1.70", "2.89", "0"]]
        fake_result.next.side_effect = [True, False]
        fake_result.get_row_data.side_effect = rows
        with patch.object(ad, "_query_with_relogin", return_value=fake_result):
            records = ad.get_daily_market("2026-08-21")
        assert len(records) == 1
        r = records[0]
        # 原样保留, 无 ×100/×1000
        assert r["volume"] == pytest.approx(51270289)
        assert r["amount"] == pytest.approx(465159863.32)
        assert r["code"] == "600000"

    def test_adjust_factor_parses_back_factor(self):
        ad = self._adapter()
        fake_result = MagicMock()
        fake_result.error_code = "0"
        fake_result.fields = ["code", "dividOperateDate", "foreAdjustFactor",
                              "backAdjustFactor", "adjustFactor"]
        fake_result.next.side_effect = [True, False]
        fake_result.get_row_data.return_value = ["sh.600519", "2026-06-26",
                                                 "1.000000", "7.669257", "7.669257"]
        with patch.object(ad, "_query_with_relogin", return_value=fake_result):
            out = ad.get_adjust_factors("600519", "2026-06-01", "2026-07-01")
        assert len(out) == 1
        assert out[0]["back_factor"] == pytest.approx(7.669257)
        assert out[0]["ex_date"] == "2026-06-26"


class TestKlineRepoRawLabel:
    def test_batch_upsert_writes_raw(self, db):
        from data_layer.repos.kline import KlineRepo
        repo = KlineRepo(db)
        repo.batch_upsert([{
            "code": "600519", "date": "2026-08-21", "open": 1270, "high": 1275,
            "low": 1268, "close": 1272.83, "volume": 1000, "amount": 1270000,
            "pre_close": 1270, "change_pct": 0.22, "turnover_rate": 0.1}])
        conn = db.get_connection()
        row = conn.execute(
            "SELECT adjust_type FROM daily_klines WHERE code='600519'").fetchone()
        assert row["adjust_type"] == "raw"

    def test_atomic_upsert_writes_raw(self, db):
        from data_layer.repos.kline import KlineRepo
        repo = KlineRepo(db)
        records = [{
            "code": f"60{i:04d}", "date": "2026-08-21", "open": 10, "high": 11,
            "low": 9, "close": 10.5, "volume": 100, "amount": 1000,
            "pre_close": 10, "change_pct": 5.0, "turnover_rate": 0.1} for i in range(5)]
        n = repo.atomic_upsert_date("2026-08-21", records, min_rows=5)
        assert n == 5
        conn = db.get_connection()
        labels = conn.execute(
            "SELECT DISTINCT adjust_type FROM daily_klines WHERE date='2026-08-21'").fetchall()
        assert [r["adjust_type"] for r in labels] == ["raw"]


class TestMootdxXdxr:
    def test_get_xdxr_matches_ex_date(self):
        from data_layer.sources.mootdx_source import MootdxSourceAdapter
        ad = MootdxSourceAdapter()
        import pandas as pd
        df = pd.DataFrame([
            {"year": 2025, "month": 12, "day": 19, "category": 1, "name": "除权除息",
             "fenhong": 239.57, "songzhuangu": 0.0, "peigujia": 0.0},
            {"year": 2026, "month": 6, "day": 26, "category": 1, "name": "除权除息",
             "fenhong": 280.24, "songzhuangu": 0.0, "peigujia": 0.0},
        ])
        fake_client = MagicMock()
        fake_client.xdxr.return_value = df
        with patch.object(ad, "_get_client", return_value=fake_client):
            out = ad.get_xdxr("600519", "2026-06-26")
        assert out is not None
        assert out["fenhong"] == pytest.approx(280.24)
        # 不存在的日期 → None
        with patch.object(ad, "_get_client", return_value=fake_client):
            assert ad.get_xdxr("600519", "2026-01-01") is None
