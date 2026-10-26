"""DL-44: is_suspended 升级 — baostock tradestatus 权威判据 + 推断降级

测试覆盖:
1. baostock 权威判据优先 (tradestatus=0 → 停牌, =1 → 未停牌)
2. baostock 不可用时降级到推断判据 (原 DL-17 逻辑)
3. baostock 返回 None (无数据) 时降级到推断判据
4. 按日批量缓存 (同日多次调用只查一次 baostock)
5. 推断判据原有场景不回归

:memory: 库, 无生产依赖。
"""

import sqlite3
from unittest.mock import MagicMock, patch

from data_layer._loader import DataLoader

DATE = "2026-08-18"


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db():
    db = _FakeDB()
    c = db.conn
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, "
              "PRIMARY KEY (code, date))")
    c.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT, "
              "list_date TEXT, is_active INTEGER DEFAULT 1, delist_date TEXT)")
    c.commit()
    return db


def _add_stock(db, code, list_date="20100101", is_active=1, delist_date=""):
    db.conn.execute(
        "INSERT INTO stock_info VALUES (?, ?, ?, ?, ?)",
        (code, f"股{code}", list_date, is_active, delist_date))
    db.conn.commit()


def _add_kline(db, code, date=DATE):
    db.conn.execute(
        "INSERT INTO daily_klines VALUES (?, ?, 10.0)", (code, date))
    db.conn.commit()


def _mk_loader_with_bs(db, status_map):
    """构造带 mock baostock 适配器的 DataLoader"""
    loader = DataLoader(db)
    mock_bs = MagicMock()
    mock_bs.get_daily_trade_status.return_value = status_map
    loader._bs_adapter = mock_bs
    loader._bs_adapter_init = True
    return loader, mock_bs


# === 场景 1: baostock 权威判据优先 ===

def test_bs_tradestatus_0_means_suspended():
    """baostock tradestatus=0 → 停牌 (即使有 kline 缺失)"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")  # 市场有行情
    loader, mock_bs = _mk_loader_with_bs(db, {"000001": False, "600519": True})
    assert loader.is_suspended("000001", DATE) is True
    mock_bs.get_daily_trade_status.assert_called_once_with(DATE)


def test_bs_tradestatus_1_means_not_suspended():
    """baostock tradestatus=1 → 未停牌 (即使无 kline)"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")  # 市场有行情, 但 000001 无 kline
    loader, mock_bs = _mk_loader_with_bs(db, {"000001": True, "600519": True})
    # baostock 说正常交易 → 未停牌 (即使推断判据会说停牌)
    assert loader.is_suspended("000001", DATE) is False


def test_bs_code_not_in_map_falls_to_inference():
    """baostock 返回的 map 中无该股 → 降级到推断判据"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    # map 里没有 000001
    loader, _ = _mk_loader_with_bs(db, {"600519": True})
    # 推断判据: 在市 + 市场开市 + 缺 kline → 停牌
    assert loader.is_suspended("000001", DATE) is True


# === 场景 2: baostock 不可用 → 降级推断 ===

def test_bs_unavailable_falls_to_inference():
    """baostock 适配器为 None → 推断判据"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True  # 跳过初始化
    assert loader.is_suspended("000001", DATE) is True


def test_bs_returns_none_falls_to_inference():
    """baostock get_daily_trade_status 返回 None → 推断判据"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    loader, mock_bs = _mk_loader_with_bs(db, None)
    mock_bs.get_daily_trade_status.return_value = None
    assert loader.is_suspended("000001", DATE) is True


def test_bs_exception_falls_to_inference():
    """baostock 抛异常 → 降级推断判据 (不崩)"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    loader, mock_bs = _mk_loader_with_bs(db, None)
    mock_bs.get_daily_trade_status.side_effect = ConnectionError("network down")
    assert loader.is_suspended("000001", DATE) is True


# === 场景 3: 按日批量缓存 ===

def test_bs_cache_same_date_single_call():
    """同日多次调用只查一次 baostock"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_stock(db, "000002")
    _add_kline(db, "600519")
    loader, mock_bs = _mk_loader_with_bs(
        db, {"000001": False, "000002": True, "600519": True})
    loader.is_suspended("000001", DATE)
    loader.is_suspended("000002", DATE)
    assert mock_bs.get_daily_trade_status.call_count == 1


def test_bs_cache_different_date_refetches():
    """不同日期重新查询"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    loader, mock_bs = _mk_loader_with_bs(db, {"000001": True, "600519": True})
    loader.is_suspended("000001", DATE)
    loader.is_suspended("000001", "2026-08-19")
    assert mock_bs.get_daily_trade_status.call_count == 2


# === 场景 4: 推断判据原有场景不回归 ===

def test_inference_true_suspension():
    """推断: 在市 + 市场开市 + 缺 kline → 停牌"""
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000001", DATE) is True


def test_inference_has_kline_not_suspended():
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "000001")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000001", DATE) is False


def test_inference_new_stock_not_suspended():
    db = _mk_db()
    _add_stock(db, "301999", list_date="20260901")
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("301999", DATE) is False


def test_inference_market_no_data_not_suspended():
    db = _mk_db()
    _add_stock(db, "000001")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000001", DATE) is False


def test_inference_delisted_not_suspended():
    db = _mk_db()
    _add_stock(db, "000002", delist_date="20260101")
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000002", DATE) is False


def test_inference_inactive_not_suspended():
    db = _mk_db()
    _add_stock(db, "000003", is_active=0)
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000003", DATE) is False


def test_inference_unknown_stock_not_suspended():
    db = _mk_db()
    _add_kline(db, "600519")
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("999999", DATE) is False


def test_missing_tables_returns_false():
    db = _FakeDB()
    loader = DataLoader(db)
    loader._bs_adapter = None
    loader._bs_adapter_init = True
    assert loader.is_suspended("000001", DATE) is False


# === 场景 5: baostock get_trade_status 单股方法 ===

def test_bs_get_trade_status_suspended():
    """baostock_source.get_trade_status: tradestatus=0 → False"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    # mock _query_with_relogin
    mock_result = MagicMock()
    mock_result.error_code = "0"
    mock_result.fields = ["date", "code", "tradestatus"]
    mock_result._qs_rows = [["2026-08-18", "sz.000001", "0"]]
    with patch.object(adapter, "_query_with_relogin", return_value=mock_result):
        with patch.object(adapter, "_drain_guarded", return_value=mock_result._qs_rows):
            result = adapter.get_trade_status("000001", DATE)
    assert result is False


def test_bs_get_trade_status_trading():
    """baostock_source.get_trade_status: tradestatus=1 → True"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    mock_result = MagicMock()
    mock_result.error_code = "0"
    mock_result.fields = ["date", "code", "tradestatus"]
    mock_result._qs_rows = [["2026-08-18", "sz.000001", "1"]]
    with patch.object(adapter, "_query_with_relogin", return_value=mock_result):
        with patch.object(adapter, "_drain_guarded", return_value=mock_result._qs_rows):
            result = adapter.get_trade_status("000001", DATE)
    assert result is True


def test_bs_get_trade_status_no_data():
    """baostock_source.get_trade_status: 无数据 → None"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    mock_result = MagicMock()
    mock_result.error_code = "0"
    mock_result.fields = ["date", "code", "tradestatus"]
    mock_result._qs_rows = []
    with patch.object(adapter, "_query_with_relogin", return_value=mock_result):
        with patch.object(adapter, "_drain_guarded", return_value=[]):
            result = adapter.get_trade_status("000001", DATE)
    assert result is None


def test_bs_get_trade_status_bj_code_returns_none():
    """北交所代码 baostock 不覆盖 → None"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    result = adapter.get_trade_status("830001", DATE)
    assert result is None


# === 场景 6: get_daily_trade_status 批量方法 ===

def test_bs_get_daily_trade_status():
    """baostock_source.get_daily_trade_status: 批量返回 map"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    mock_result = MagicMock()
    mock_result.error_code = "0"
    mock_result.fields = ["date", "code", "tradestatus"]
    mock_result._qs_rows = [
        ["2026-08-18", "sz.000001", "1"],
        ["2026-08-18", "sz.000002", "0"],
        ["2026-08-18", "sh.600519", "1"],
    ]
    with patch.object(adapter, "_query_with_relogin", return_value=mock_result):
        with patch.object(adapter, "_drain_guarded", return_value=mock_result._qs_rows):
            result = adapter.get_daily_trade_status(DATE)
    assert result == {"000001": True, "000002": False, "600519": True}


def test_bs_get_daily_trade_status_error_returns_none():
    """baostock 查询失败 → None"""
    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    adapter = BaoStockSourceAdapter()
    mock_result = MagicMock()
    mock_result.error_code = "1"
    mock_result.error_msg = "server error"
    with patch.object(adapter, "_query_with_relogin", return_value=mock_result):
        result = adapter.get_daily_trade_status(DATE)
    assert result is None
