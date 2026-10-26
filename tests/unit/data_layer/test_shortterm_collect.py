"""DL-07: 不可回填数据采集 — 端点方法/适配器分发/管线落库/读 API/新鲜度测试

mock client + :memory: 库, 无网络无生产库。
"""

import sqlite3
from unittest.mock import Mock

import pytest

from data_layer.sources.fuyao_client import FuyaoSourceAdapter


# --- 适配器能力与分发 ---

def test_capabilities_include_collect_types():
    caps = FuyaoSourceAdapter().capabilities
    for t in ("limit_ladder", "limit_break", "hot_stock_history",
              "skyrocket", "auction_benchmark", "auction_snapshot"):
        assert t in caps, f"missing capability {t}"


def test_adapter_dispatch_limit_break():
    client = Mock()
    client.get_limit_break_pool = Mock(return_value=[
        {"thscode": "301156.SZ", "ticker": "301156", "name": "美农生物",
         "last_price": 13.03, "price_change_ratio_pct": 11.06,
         "open_times": 1, "turnover_ratio_pct": 8.97, "turnover": 137791340.0}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("limit_break", {"date": "2026-08-18"})
    assert len(rows) == 1
    assert rows[0]["ticker"] == "301156"
    client.get_limit_break_pool.assert_called_once_with("2026-08-18")


def test_adapter_dispatch_skyrocket():
    client = Mock()
    client.get_skyrocket_list = Mock(return_value=[
        {"thscode": "600206.SH", "ticker": "600206", "name": "有研新材",
         "rank": 1, "heat": "25578.9", "rank_change": 0, "rank_trend": "flat"}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("skyrocket", {})
    assert rows[0]["rank"] == 1


def test_adapter_dispatch_ladder_returns_dict():
    client = Mock()
    client.get_limit_up_ladder = Mock(return_value={"window": {}, "item": []})
    adapter = FuyaoSourceAdapter(client=client)
    result = adapter.get_data("limit_ladder", {"date": "2026-08-18"})
    assert isinstance(result, dict)


def test_adapter_dispatch_auction_snapshot():
    client = Mock()
    client.get_auction_snapshot = Mock(return_value=[
        {"thscode": "688825.SH", "ticker": "688825", "auction_price": 61.15}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("auction_snapshot", {"codes": "688825.SH"})
    assert rows[0]["auction_price"] == 61.15
    client.get_auction_snapshot.assert_called_once_with("688825.SH")


def test_anomaly_uses_correct_endpoint():
    """DL-07: 旧 /anomaly-list 已 404, get_anomaly_list 必须走 anomaly-analysis-list"""
    import inspect
    from data_layer.sources.fuyao_client import FuyaoClient
    src = inspect.getsource(FuyaoClient.get_anomaly_list)
    assert "anomaly-analysis-list" in src
    assert '"/api/a-share/special-data/anomaly-list"' not in src


# --- 管线落库 (mock registry + :memory: db) ---

class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


class _FakeSource:
    name = "fuyao"

    def __init__(self, data_map):
        self._data = data_map

    def get_data(self, data_type, params):
        return self._data.get(data_type)


class _FakeRegistry:
    def __init__(self, source):
        self._source = source

    def get_sources_for(self, data_type):
        return [self._source]


def _make_pipeline(data_map):
    from data_layer.pipelines.shortterm_collect import ShortTermCollectPipeline
    db = _FakeDB()
    pipe = ShortTermCollectPipeline(db=db, source_registry=_FakeRegistry(_FakeSource(data_map)))
    return pipe, db


def test_pipeline_collects_all_types():
    from data_layer.pipelines.base import PipelineInput
    data_map = {
        "limit_ladder": {
            "window": {"length": 30, "date_list": ["2026-08-18"],
                       "board_caps": {"two_board": 2}},
            "item": [{"date": "2026-08-18", "boards": {"two_board": [
                {"thscode": "000505.SZ", "ticker": "000505", "name": "京粮控股",
                 "board_num": 2, "seal_nextday": None, "sign_level": 0}]}}],
        },
        "limit_break": [{"thscode": "301156.SZ", "ticker": "301156", "name": "美农生物",
                         "last_price": 13.03, "price_change_ratio_pct": 11.06,
                         "open_times": 1, "turnover_ratio_pct": 8.97, "turnover": 1.3e8}],
        "hot_stock_history": [{"thscode": "688825.SH", "ticker": "688825",
                               "name": "长鑫科技", "rank": 1}],
        "skyrocket": [{"thscode": "600206.SH", "ticker": "600206", "name": "有研新材",
                       "rank": 1, "heat": "25578.9", "rank_change": 0, "rank_trend": "flat"}],
        "anomaly": [{"stock_name": "贝达药业", "thscode": "300558.SZ", "tag_name": "跌停",
                     "analysis_content": "涨幅较大", "keyword_list": ["涨幅较大"]}],
        "sector_fund_flow": [{"sector_name": "农林牧渔", "sector_type": "industry",
                              "change_pct": 4.36, "main_net_amount": 2.0e9,
                              "main_net_ratio": 9.18, "top_stock": "神农种业"}],
    }
    pipe, db = _make_pipeline(data_map)
    result = pipe.execute(PipelineInput(date="2026-08-18"))
    assert result.status == "success", result.errors
    assert result.records_written > 0
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM limit_up_ladder").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM limit_up_ladder_meta").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM limit_break_pool").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hot_stock_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM skyrocket_list").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM stock_anomaly_reasons").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sector_fund_flow").fetchone()[0] == 1
    # 来源记录 (最低治理)
    assert conn.execute("SELECT source FROM limit_break_pool").fetchone()[0] == "fuyao"
    assert conn.execute("SELECT source FROM sector_fund_flow").fetchone()[0] == "akshare"


def test_pipeline_partial_on_source_failure():
    from data_layer.pipelines.base import PipelineInput
    # 只有 skyrocket 有数据, 其余源失败 → partial
    pipe, db = _make_pipeline({"skyrocket": [
        {"thscode": "600206.SH", "ticker": "600206", "name": "有研新材", "rank": 1}]})
    result = pipe.execute(PipelineInput(date="2026-08-18"))
    assert result.status == "partial"
    assert result.records_written == 1
    assert len(result.errors) == 5  # 其余 5 项无数据


def test_pipeline_idempotent_rerun():
    from data_layer.pipelines.base import PipelineInput
    data_map = {"skyrocket": [
        {"thscode": "600206.SH", "ticker": "600206", "name": "有研新材", "rank": 1}]}
    pipe, db = _make_pipeline(data_map)
    pipe.execute(PipelineInput(date="2026-08-18"))
    pipe.execute(PipelineInput(date="2026-08-18"))
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM skyrocket_list").fetchone()[0] == 1


# --- 读 API ---

def test_read_apis_return_dicts():
    from data_layer._loader import DataLoader
    from data_layer.pipelines.base import PipelineInput
    data_map = {
        "limit_break": [{"thscode": "301156.SZ", "ticker": "301156", "name": "美农生物",
                         "last_price": 13.03, "open_times": 1}],
        "hot_stock_history": [{"thscode": "688825.SH", "ticker": "688825",
                               "name": "长鑫科技", "rank": 1}],
        "sector_fund_flow": [{"sector_name": "农林牧渔", "sector_type": "industry",
                              "main_net_amount": 2.0e9}],
    }
    pipe, db = _make_pipeline(data_map)
    pipe.execute(PipelineInput(date="2026-08-18"))
    loader = DataLoader(db)
    assert len(loader.get_limit_break_pool("2026-08-18")) == 1
    assert loader.get_limit_break_pool("2026-08-18")[0]["code"] == "301156"
    assert loader.get_hot_stock_history("2026-08-18")[0]["rank"] == 1
    flows = loader.get_sector_fund_flow("2026-08-18", sector_type="industry")
    assert flows[0]["sector_name"] == "农林牧渔"
    # 空日期返回 []
    assert loader.get_limit_break_pool("2020-01-01") == []


def test_read_apis_missing_table_returns_empty():
    """表未创建 (管线未首跑) 时读 API 返回空而非抛错"""
    from data_layer._loader import DataLoader
    db = _FakeDB()
    loader = DataLoader(db)
    assert loader.get_limit_break_pool("2026-08-18") == []
    assert loader.get_skyrocket_list("2026-08-18") == []
    assert loader.get_anomaly_reasons("2026-08-18") == []
    assert loader.get_limit_ladder("2026-08-18") == {"meta": None, "days": []}
    assert loader.get_auction_data("2026-08-18") == {"benchmark": [], "snapshot": []}


# --- 新鲜度检查 ---

def test_collect_freshness_check():
    from data_layer.health.readiness import DataReadiness
    from data_layer.pipelines.base import PipelineInput
    pipe, db = _make_pipeline({"skyrocket": [
        {"thscode": "600206.SH", "ticker": "600206", "name": "有研新材", "rank": 1}]})
    pipe.execute(PipelineInput(date="2026-08-18"))
    readiness = DataReadiness(db)
    fresh = readiness.check_collect_freshness("2026-08-18")
    assert fresh["skyrocket_list"]["fresh"] is True
    assert fresh["skyrocket_list"]["rows"] == 1
    assert fresh["limit_break_pool"]["fresh"] is False  # 表存在但当日无行
    assert fresh["auction_snapshot"]["fresh"] is False  # 表不存在


# --- akshare 板块资金流适配器 ---

def test_akshare_sector_fund_flow_mapping():
    """#116 面二 swap: 10jqka 列形状映射 (探针 2026-09-05 01:37 实测列名)。"""
    from data_layer.sources.akshare_source import AkShareSourceAdapter
    import pandas as pd
    adapter = AkShareSourceAdapter()
    fake_ak = Mock()
    df = pd.DataFrame([{
        "序号": 1, "行业": "农林牧渔", "行业-涨跌幅": 4.36,
        "行业指数": 1234.5, "流入资金": 2.6e9, "流出资金": 0.6e9,
        "净额": 2.0e9, "公司家数": 88, "领涨股": "神农种业",
        "领涨股-涨跌幅": 9.99, "当前价": 12.34,
    }])
    fake_ak.stock_fund_flow_industry = Mock(return_value=df)
    fake_ak.stock_fund_flow_concept = Mock(return_value=df)
    rows = adapter._get_sector_fund_flow(fake_ak, {"date": "2026-08-18"})
    # 行业+概念两个端点各调用一次 (10jqka 双端点, symbol="即时")
    fake_ak.stock_fund_flow_industry.assert_called_once_with(symbol="即时")
    fake_ak.stock_fund_flow_concept.assert_called_once_with(symbol="即时")
    assert len(rows) == 2
    assert rows[0]["sector_name"] == "农林牧渔"
    assert rows[0]["sector_type"] == "industry"
    assert rows[0]["main_net_amount"] == 2.0e9
    assert rows[0]["change_pct"] == 4.36
    assert rows[0]["top_stock"] == "神农种业"
    # 10jqka 不供占比/大小单拆分 → 宁 NULL 不假值 (#116/#122)
    for r in rows:
        for k in ("main_net_ratio", "super_large_amount", "large_amount",
                  "medium_amount", "small_amount"):
            assert r[k] is None


