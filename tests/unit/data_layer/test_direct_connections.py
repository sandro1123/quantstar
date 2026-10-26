"""DL-08: 收编直连 — 快照端点修复/适配器归一化/snapshot_intraday走registry/新读API

mock client + :memory: 库, 无网络无生产库。
"""

import sqlite3
from unittest.mock import Mock

from data_layer.sources.fuyao_client import FuyaoClient, FuyaoSourceAdapter

# --- FuyaoClient 快照端点修复 (返回 dict.item 而非 list) ---

def test_price_snapshot_unwraps_item():
    """DL-08: 端点返回 {"item": [...]} dict, 旧实现 isinstance(list) 恒 False → 恒 []"""
    client = FuyaoClient(api_key="test")
    client._get = Mock(return_value={
        "item": [{"thscode": "600519.SH", "ticker": "600519", "last_price": 1290.69}],
        "timestamp": 1, "total": 1})
    rows = client.get_price_snapshot("600519.SH")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "600519"

def test_price_snapshot_list_compat():
    client = FuyaoClient(api_key="test")
    client._get = Mock(return_value=[{"thscode": "600519.SH"}])
    assert len(client.get_price_snapshot("600519.SH")) == 1

def test_price_snapshot_none():
    client = FuyaoClient(api_key="test")
    client._get = Mock(return_value=None)
    assert client.get_price_snapshot("600519.SH") == []

def test_index_snapshot_unwraps_item():
    client = FuyaoClient(api_key="test")
    client._get = Mock(return_value={
        "item": [{"thscode": "000001.SH", "last_price": 3966.99}], "total": 1})
    rows = client.get_index_snapshot("000001.SH")
    assert len(rows) == 1
    assert rows[0]["last_price"] == 3966.99

# --- 适配器归一化: limit_up/limit_down → 统一契约 (与 akshare 同构) ---

_FUYAO_LIMIT_ROW = {
    "thscode": "002820.SZ", "ticker": "002820", "name": "桂发祥",
    "last_price": 11.15, "price_change_ratio_pct": 9.96,
    "continue_day_cnt": 2, "seal_money": 1e8,
}

def test_limit_up_normalized():
    client = Mock()
    client.get_limit_up_pool = Mock(return_value=[_FUYAO_LIMIT_ROW])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("limit_up", {"date": "2026-08-18"})
    assert rows == [{
        "code": "002820", "name": "桂发祥", "date": "2026-08-18",
        "limit_type": "up", "price": 11.15, "change_pct": 9.96, "count": 2,
    }]

def test_limit_down_normalized():
    client = Mock()
    client.get_limit_down_pool = Mock(return_value=[
        {**_FUYAO_LIMIT_ROW, "ticker": "300001", "thscode": "300001.SZ"}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("limit_down", {"date": "2026-08-18"})
    assert rows[0]["limit_type"] == "down"
    assert rows[0]["code"] == "300001"

def test_limit_up_code_from_thscode_when_no_ticker():
    client = Mock()
    client.get_limit_up_pool = Mock(return_value=[
        {"thscode": "600519.SH", "name": "贵州茅台", "last_price": 1290.0,
         "price_change_ratio_pct": 10.0, "continue_day_cnt": 1}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("limit_up", {"date": "2026-08-18"})
    assert rows[0]["code"] == "600519"

def test_limit_up_skips_no_code():
    client = Mock()
    client.get_limit_up_pool = Mock(return_value=[{"name": "无码"}])
    adapter = FuyaoSourceAdapter(client=client)
    assert adapter.get_data("limit_up", {"date": "2026-08-18"}) == []

# --- 适配器归一化: realtime_quote → 统一契约 (与 gtimg 同构) ---

def test_realtime_quote_normalized():
    client = Mock()
    client.get_price_snapshot = Mock(return_value=[{
        "thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
        "last_price": 1290.69, "price_change_ratio_pct": -0.19,
        "open_price": 1291.0, "high_price": 1298.86, "low_price": 1285.17,
        "volume": 2297174, "turnover": 2963670100, "prev_price": 1293.09}])
    adapter = FuyaoSourceAdapter(client=client)
    rows = adapter.get_data("realtime_quote", {"codes": "600519.SH"})
    assert rows[0]["code"] == "600519"
    assert rows[0]["price"] == 1290.69
    assert rows[0]["change_pct"] == -0.19
    assert rows[0]["amount"] == 2963670100
    assert rows[0]["pre_close"] == 1293.09

# --- snapshot_intraday 走 SourceRegistry (不再硬编码 FuyaoClient) ---

class _FakeDB:
    def __init__(self):
        # check_same_thread=False 对齐真实 Database (get_intraday_news 经
        # @with_timeout 在后台线程执行)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE daily_klines (code TEXT, date TEXT, change_pct REAL)")

    def get_connection(self):
        return self.conn

class _FakeSource:
    def __init__(self, name, caps, data_map):
        self.name = name
        self.capabilities = caps
        self._data = data_map

    def is_available(self):
        return True

    def get_data(self, data_type, params):
        return self._data.get(data_type)

class _FakeRegistry:
    def __init__(self, sources):
        self._sources = sources

    def get_sources_for(self, data_type):
        return [s for s in self._sources if data_type in s.capabilities]

def test_snapshot_intraday_uses_registry_not_raw_client():
    """DL-08: _loader 不再 import/实例化裸 FuyaoClient (docstring 提及不算)"""
    import inspect
    from data_layer import _loader
    src = inspect.getsource(_loader.DataLoader.snapshot_intraday)
    assert "= FuyaoClient()" not in src
    assert "import FuyaoClient" not in src
    assert "except Exception:\n            pass" not in src

def test_snapshot_intraday_full_flow():
    from data_layer._loader import DataLoader
    fuyao = _FakeSource("fuyao", {"hot_stock", "limit_up", "limit_down"}, {
        "hot_stock": [{"thscode": "600519.SH", "ticker": "600519",
                       "name": "贵州茅台", "rank": 1, "heat": 9999.0}],
        "limit_up": [{"code": "002820", "name": "桂发祥", "date": "2026-08-18",
                      "limit_type": "up", "price": 11.15, "change_pct": 9.96,
                      "count": 2}],
        "limit_down": [],
    })
    gtimg = _FakeSource("gtimg", {"realtime_quote_batch"}, {
        "realtime_quote_batch": [{"code": "600519", "name": "贵州茅台",
                                   "price": 1290.69, "change_pct": -0.19,
                                   "open": 1291.0, "high": 1298.86,
                                   "low": 1285.17, "volume": 2.3e6,
                                   "amount": 2.96e9}],
    })
    db = _FakeDB()
    loader = DataLoader(db)
    snap = loader.snapshot_intraday(
        timestamp="2026-08-18 11:00:00",
        source_registry=_FakeRegistry([fuyao, gtimg]))
    assert len(snap.hot_stocks) == 1
    assert snap.hot_stocks[0].code == "600519"
    assert len(snap.realtime_quotes) == 1
    assert snap.realtime_quotes[0].current == 1290.69
    assert len(snap.limit_up_down) == 1
    assert snap.limit_up_down[0].code == "002820"
    assert snap.limit_up_down[0].limit_type == "涨停"

def test_snapshot_intraday_source_failure_degrades():
    """源失败 → 记日志降级为空, 不抛错不吞错"""
    from data_layer._loader import DataLoader

    class _BoomSource(_FakeSource):
        def get_data(self, data_type, params):
            raise RuntimeError("boom")

    boom = _BoomSource("fuyao", {"hot_stock", "limit_up", "limit_down"}, {})
    db = _FakeDB()
    loader = DataLoader(db)
    snap = loader.snapshot_intraday(
        timestamp="2026-08-18 11:00:00", source_registry=_FakeRegistry([boom]))
    assert snap.hot_stocks == []
    assert snap.realtime_quotes == []
    assert snap.limit_up_down == []

def test_snapshot_intraday_no_registry():
    """无 registry → 各实时字段为空, 不崩"""
    from data_layer._loader import DataLoader
    db = _FakeDB()
    loader = DataLoader(db)
    snap = loader.snapshot_intraday(timestamp="2026-08-18 11:00:00")
    assert snap.hot_stocks == []
    assert snap.realtime_quotes == []

# --- _fetch_realtime_quotes: 批量优先 + 逐只兜底 ---

def test_fetch_realtime_quotes_batch_first():
    from data_layer._loader import DataLoader
    gtimg = _FakeSource("gtimg", {"realtime_quote_batch"}, {
        "realtime_quote_batch": [{"code": "600519", "price": 1290.0}]})
    loader = DataLoader(_FakeDB())
    rows = loader._fetch_realtime_quotes(["600519"], _FakeRegistry([gtimg]))
    assert rows[0]["code"] == "600519"

def test_fetch_realtime_quotes_single_fallback():
    from data_layer._loader import DataLoader
    single = _FakeSource("gtimg", {"realtime_quote"}, {
        "realtime_quote": {"code": "600519", "price": 1290.0}})
    loader = DataLoader(_FakeDB())
    rows = loader._fetch_realtime_quotes(["600519"], _FakeRegistry([single]))
    assert rows[0]["price"] == 1290.0

# --- DataLayer 新读 API ---

def test_get_realtime_indices_via_registry():
    """DL-08: 报告层 gtimg 直连的收编入口"""
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)  # 跳过 __init__ 的重初始化
    dl._source_registry = _FakeRegistry([
        _FakeSource("gtimg", {"index"}, {
            "index": [{"name": "上证", "price": 3966.99, "change_pct": -0.39}]})])
    rows = dl.get_realtime_indices()
    assert rows[0]["name"] == "上证"

def test_get_realtime_indices_no_source():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    dl._source_registry = _FakeRegistry([])
    assert dl.get_realtime_indices() == []

def test_get_intraday_news_reads_collector():
    """DL-08: NewsCollector 只经 DataLayer 方法访问"""
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT, source TEXT, "
        "sentiment TEXT, sentiment_score REAL, summary TEXT)")
    db.conn.execute(
        "INSERT INTO stock_news VALUES ('', '2026-08-18 10:00:00', '测试新闻', "
        "'em', 'neutral', 0, '')")
    db.conn.commit()
    dl.db = db
    items = dl.get_intraday_news("2026-08-18", limit=5, collect_if_empty=False)
    assert len(items) == 1
    assert items[0]["title"] == "测试新闻"
