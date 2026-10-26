"""DL-32 Part 2: 17 个数据层管线逐个断言 dry_run=True 时目标表行数不变。

每条测试通过管线真实的取数缝 (fetch seam) 喂入最小数据, 迫使 run() 走到写库分支,
再断言: ① 目标表行数不变 ② metadata.dry_run_writes_blocked >= 1 (证明拦截真发生,
非空跑)。valuation 已有自己的 dry_run 实现 (跳过 batch_upsert), 与基类拦截共存不冲突,
故只断言行数不变 + records_written==0。

这是事故 05 的防复发条款: 契约字段必须在基类强制, 且有测试逐个覆盖 17 管线。
"""
from datetime import datetime, timezone

import pytest

from common.database import Database
from common.utils import bj_date
from data_layer.pipelines.base import PipelineInput

DATE = "2026-08-22"


@pytest.fixture
def db(test_db_path):
    d = Database(test_db_path)
    d.init_db()
    # 多个管线 run() 内会 SELECT stock_info (analyst_forecast 等); 建空表避免
    # "no such table" 把 run() 打成异常 (那会让 writes_blocked 恒 0, 测试变空跑)。
    d.get_connection().execute(
        "CREATE TABLE IF NOT EXISTS stock_info (code TEXT PRIMARY KEY, name TEXT, "
        "industry TEXT, market TEXT, list_date TEXT, is_active INTEGER)"
    )
    d.get_connection().commit()
    return d


class _FakeRegistry:
    """最小 SourceRegistry: get_sources_for 返回一个占位源。"""
    def get_sources_for(self, capability):
        return [object()]


def _count(db, table):
    try:
        return db.get_connection().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _run_dry(pipe, db, **kw):
    inp = PipelineInput(date=kw.pop("date", DATE), db=db, dry_run=True, **kw)
    return pipe.execute(inp)


# --- 直连 conn 类管线 ---

def test_lhb_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.lhb import LHBPipeline
    # 卡②: _fetch 契约扩为 4 元组 (rows, src, warnings, fetch_error)
    monkeypatch.setattr(LHBPipeline, "_fetch",
                        lambda self, d: ([{"stock_code": "000001", "buy_in": 1.0,
                                           "turnover": 2.0, "capitalization": 3.0}],
                                         "fake", [], ""))
    res = _run_dry(LHBPipeline(), db)
    assert _count(db, "lhb_daily") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_analyst_forecast_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.analyst_forecast import AnalystForecastPipeline
    monkeypatch.setattr(AnalystForecastPipeline, "_fetch_for_code",
                        lambda self, c: {"consensus_score": 1.0, "target_price": 10.0, "rating": "buy"})
    res = _run_dry(AnalystForecastPipeline(), db)
    assert _count(db, "analyst_forecasts") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_concept_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.concept import ConceptPipeline

    def fake_fetch(self, data_type, params):
        if data_type == "limit_up":
            return [{"plate_name": "AI", "stocks": [{"stock_code": "000001"}]}]
        return []
    monkeypatch.setattr(ConceptPipeline, "_fetch", fake_fetch)
    res = _run_dry(ConceptPipeline(), db)
    assert _count(db, "concept_daily") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_sentiment_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.sentiment import SentimentPipeline
    monkeypatch.setattr(SentimentPipeline, "_fetch",
                        lambda self, d: ({"info": {"sign": "50", "ZT": 10, "DT": 5,
                                                   "SJZT": 100, "SJDT": 80}}, "fake"))
    res = _run_dry(SentimentPipeline(), db)
    assert _count(db, "market_sentiment") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_limit_up_down_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.limit_up_down import LimitUpDownPipeline

    def fake_pool(self, data_type, date):
        if data_type == "limit_up":
            return [{"code": "000001", "name": "X", "limit_type": "up",
                     "price": 10.0, "change_pct": 10.0, "count": 1}], []
        return [], []
    monkeypatch.setattr(LimitUpDownPipeline, "_fetch_pool", fake_pool)
    res = _run_dry(LimitUpDownPipeline(), db)
    assert _count(db, "stock_limit_up_down") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_auction_collect_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.auction_collect import AuctionCollectPipeline

    def fake_fetch(self, data_type, params):
        if data_type == "auction_benchmark":
            return [{"ticker": "000001", "name": "X", "auction_pct": 1.0}], "fake"
        return [], ""
    monkeypatch.setattr(AuctionCollectPipeline, "_fetch", fake_fetch)
    res = _run_dry(AuctionCollectPipeline(), db)
    assert _count(db, "auction_benchmark") == 0
    assert _count(db, "auction_snapshot") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_shortterm_collect_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.shortterm_collect import ShortTermCollectPipeline

    def fake_fetch(self, data_type, params):
        if data_type == "limit_ladder":
            return {"window": {"length": 3}, "item": []}, "fake"
        return None, ""
    monkeypatch.setattr(ShortTermCollectPipeline, "_fetch", fake_fetch)
    res = _run_dry(ShortTermCollectPipeline(), db)
    assert _count(db, "limit_up_ladder_meta") == 0
    assert _count(db, "limit_up_ladder") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_index_daily_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.index_daily import IndexDailyPipeline
    monkeypatch.setattr(IndexDailyPipeline, "_fetch",
                        lambda self, s: [{"date": DATE, "open": 1.0, "high": 2.0,
                                          "low": 0.5, "close": 1.5, "volume": 100.0}])
    res = _run_dry(IndexDailyPipeline(), db)
    assert _count(db, "index_daily") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


# --- 读本地库即写类管线 (无外部 fetch) ---

def test_freshness_audit_dry_run_no_write(db):
    # DataReadiness.check 对 REQUIRED_TABLES 直接 SELECT COUNT, 表缺失会抛异常;
    # 建空表让 check() 走完, 才能到达写 freshness_audit 的分支。
    conn = db.get_connection()
    for t in ("daily_klines", "stock_financials", "stock_valuation"):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (code TEXT, date TEXT)")
    conn.commit()
    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
    res = _run_dry(FreshnessAuditPipeline(), db)
    assert _count(db, "freshness_audit") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_quality_check_dry_run_no_write(db):
    # DataQuality.report 对 daily_klines 查 open/high/low/close 等列; 建带列空表
    # 让 report() 走完, 才能到达写 quality_checks 的分支。
    conn = db.get_connection()
    conn.execute("DROP TABLE IF EXISTS daily_klines")
    conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, volume REAL, amount REAL, pre_close REAL, change_pct REAL)"
    )
    conn.commit()
    from data_layer.pipelines.quality_check import QualityCheckPipeline
    res = _run_dry(QualityCheckPipeline(), db)
    assert _count(db, "quality_checks") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


# --- 经 collector / repo 类管线 ---

def test_news_dry_run_no_write(db, monkeypatch):
    from data_layer.news_collector import NewsCollector
    from data_layer.pipelines.news import NewsPipeline
    item = {"title": "A股大涨", "summary": "沪指上涨", "date": DATE, "source": "em"}
    monkeypatch.setattr(NewsCollector, "_fetch_global_em", lambda self, d: [dict(item)])
    monkeypatch.setattr(NewsCollector, "_fetch_caixin", lambda self, d: [])
    monkeypatch.setattr(NewsCollector, "_fetch_cctv", lambda self, d: [])
    # 直通去重/噪音过滤, 保证条目必达 _persist (写库分支)
    monkeypatch.setattr(NewsCollector, "_deduplicate", lambda self, items: items)
    monkeypatch.setattr(NewsCollector, "_filter_noise", lambda self, items: items)
    res = _run_dry(NewsPipeline(), db)  # news 走 self.db, 基类须同时接管
    assert _count(db, "stock_news") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_stock_list_dry_run_no_write(db, monkeypatch):
    import data_layer.sources.akshare_sina_source as sina_mod

    class FakeSina:
        def get_data(self, data_type, params):
            return [{"code": "000001", "name": "平安银行"}]
    monkeypatch.setattr(sina_mod, "AkshareSinaAdapter", FakeSina)
    # tushare 富化分支: 令 is_available 返回 False 走回落
    import data_layer.sources.tushare_source as ts_mod
    monkeypatch.setattr(ts_mod.TushareSourceAdapter, "is_available", lambda self: False)

    from data_layer.pipelines.stock_list import StockListPipeline
    res = _run_dry(StockListPipeline(), db)
    assert _count(db, "stock_info") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_financial_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.financial import FinancialPipeline
    monkeypatch.setattr(FinancialPipeline, "_fetch_financial",
                        lambda self, c, rd: {"code": c, "date": "2026-06-30", "eps": 1.0,
                                             "roe": 0.1, "revenue": 1e6, "profit": 1e5})
    res = _run_dry(FinancialPipeline(), db, symbols=["000001"])
    assert _count(db, "stock_financials") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_moneyflow_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.moneyflow import MoneyflowPipeline
    monkeypatch.setattr(MoneyflowPipeline, "_get_all_flows",
                        lambda self: {"000001": {"main_net_inflow": 1.0,
                                                 "main_inflow": 2.0, "main_outflow": 1.0}})
    # moneyflow 仅接受当日; run() 会调 source_registry.get_sources_for, 需真 stub
    pipe = MoneyflowPipeline(source_registry=_FakeRegistry())
    res = _run_dry(pipe, db, date=bj_date(), symbols=["000001"])
    assert _count(db, "stock_moneyflow") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_kline_dry_run_no_write(db, monkeypatch):
    from data_layer.pipelines.kline import KlinePipeline

    # run() 期望 _fetch_kline 返回 dict 记录 (真实实现内部已把 Kline 转 dict)。
    def fake_fetch(self, repo, code, s, e):
        return 1, [{"code": code, "date": DATE, "open": 1.0, "high": 2.0, "low": 0.5,
                    "close": 1.5, "volume": 100.0, "amount": 150.0, "pre_close": 1.0,
                    "change_pct": 50.0, "turnover_rate": 1.0}], None
    monkeypatch.setattr(KlinePipeline, "_fetch_kline", fake_fetch)
    res = _run_dry(KlinePipeline(), db, symbols=["000001"])
    assert _count(db, "daily_klines") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1


def test_valuation_dry_run_no_write(db, monkeypatch):
    """valuation 自带 dry_run (跳过 batch_upsert), 与基类拦截共存; 断言行数不变。"""
    from data_layer.pipelines.valuation import ValuationPipeline
    monkeypatch.setattr(ValuationPipeline, "_fetch_batch",
                        lambda self, source, date, symbols, m, timeout_s=None:
                        [{"code": "000001", "date": DATE, "pe_ttm": 10.0, "pb_mrq": 1.0}])

    class FakeReg:
        def get_sources_for(self, cap):
            return [object()]
    res = _run_dry(ValuationPipeline(source_registry=FakeReg()), db, symbols=["000001"])
    assert _count(db, "stock_valuation") == 0
    assert res.records_written == 0  # valuation 自己的 dry_run 语义


def test_fuyao_dry_run_no_write(db, monkeypatch):
    import data_layer.pipelines.fuyao as fuyao_mod
    from data_layer.pipelines.fuyao import FuyaoPipeline

    period_ms = int(datetime(2026, 3, 31, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(fuyao_mod, "_detect_best_report", lambda client, probe_code="000001": "2026Q1")
    monkeypatch.setattr(fuyao_mod, "_report_period_date",
                        lambda client, report, probe_code="000001": "2026-03-31")

    class FakeClient:
        api_key = "test-key"
        def available(self):
            return True
        def get_income_statements(self, ths, report=None):
            return [{"basic_eps": 1.0, "operating_income": 1e6,
                     "parent_holder_net_profit": 1e5, "period_end_ms": period_ms}]
        def get_balance_sheets(self, ths, report=None):
            return [{"assets_total": 1e7, "total_debt": 1e6, "holder_equity_total": 9e6}]
        def get_cash_flow(self, ths, report=None):
            return [{"act_cash_flow_net": 1e5}]
        def get_financial_indicators(self, ths, report=None):
            # DL-76(fb32c7e) 后空 abilities 会被管线跳过写入 → 无物可拦,
            # dry_run_writes_blocked 恒 0, 本测试(事故05防复发条款)失效。
            # 故必须喂非空 indicators 迫使走到写库分支, 证明基类拦截真发生;
            # 「空数据不写」的业务语义归 test_dl76_indicators_guard.py 管。
            return {"abilities": [{"indicators": [
                {"index_id": "index_weighted_avg_roe", "value": 10.0},
                {"index_id": "sale_gross_margin", "value": 89.76},
                {"index_id": "sale_net_interest_ratio", "value": 52.22},
            ]}]}

    pipe = FuyaoPipeline()
    pipe._client = FakeClient()
    res = _run_dry(pipe, db, symbols=["000001"])
    assert _count(db, "stock_financials") == 0
    assert res.metadata["dry_run_writes_blocked"] >= 1
