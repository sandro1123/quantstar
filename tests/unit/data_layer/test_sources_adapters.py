"""测试数据源适配器"""

import pytest
from unittest.mock import Mock, patch


def test_fuyao_source_capabilities():
    """测试 FuyaoSourceAdapter 能力声明"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    adapter = FuyaoSourceAdapter()
    assert adapter.name == "fuyao"
    assert "hot_stock" in adapter.capabilities
    assert "anomaly" in adapter.capabilities
    assert "limit_up" in adapter.capabilities


def test_fuyao_source_is_available_without_key():
    """无 API key 时 FuyaoSourceAdapter 不可用"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    adapter = FuyaoSourceAdapter()
    with patch.dict("os.environ", {}, clear=True):
        assert adapter.is_available() is False


def test_fuyao_source_is_available_with_key():
    """有 API key 时 FuyaoSourceAdapter 可用"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    mock_client = Mock()
    mock_client.available.return_value = True
    adapter = FuyaoSourceAdapter(client=mock_client)
    assert adapter.is_available() is True


def test_fuyao_source_get_data_financial():
    """测试 FuyaoSourceAdapter 获取财务数据"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    mock_client = Mock()
    mock_client.get_financial_indicators.return_value = {"abilities": {"eps": 1.5}}
    adapter = FuyaoSourceAdapter(client=mock_client)

    result = adapter.get_data("financial", {"code": "600519.SH"})
    assert result == {"abilities": {"eps": 1.5}}
    mock_client.get_financial_indicators.assert_called_once_with(
        "600519.SH", period="quarterly", limit=1
    )


def test_fuyao_source_get_data_hot_stock():
    """测试 FuyaoSourceAdapter 获取热股数据"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    mock_client = Mock()
    mock_client.get_hot_stock_list.return_value = [{"code": "000001.SZ"}]
    adapter = FuyaoSourceAdapter(client=mock_client)

    result = adapter.get_data("hot_stock", {})
    assert result == [{"code": "000001.SZ"}]
    mock_client.get_hot_stock_list.assert_called_once()


def test_fuyao_source_get_data_limit_up():
    """测试 FuyaoSourceAdapter 获取涨停数据 — DL-08 归一化为统一契约"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    mock_client = Mock()
    mock_client.get_limit_up_pool.return_value = [{
        "thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
        "last_price": 11.5, "price_change_ratio_pct": 10.0, "continue_day_cnt": 1,
    }]
    adapter = FuyaoSourceAdapter(client=mock_client)

    result = adapter.get_data("limit_up", {"date": "2025-01-01"})
    assert result == [{
        "code": "000001", "name": "平安银行", "date": "2025-01-01",
        "limit_type": "up", "price": 11.5, "change_pct": 10.0, "count": 1,
    }]
    mock_client.get_limit_up_pool.assert_called_once_with("2025-01-01")


def test_fuyao_source_get_data_anomaly_unsupported():
    """测试 FuyaoSourceAdapter anomaly 类型 — get_anomaly_list 不存在时抛出 AttributeError"""
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    mock_client = Mock(spec=["available", "get_financial_indicators", "get_hot_stock_list", "get_limit_up_pool"])
    adapter = FuyaoSourceAdapter(client=mock_client)

    with pytest.raises(AttributeError):
        adapter.get_data("anomaly", {"tag_codes": []})


def test_fuyao_source_get_data_unknown_type():
    """测试 FuyaoSourceAdapter 未知数据类型抛 QuantStarError (DL-05: 原静默返回 None)"""
    from common.errors import QuantStarError
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter

    adapter = FuyaoSourceAdapter()
    with pytest.raises(QuantStarError):
        adapter.get_data("unknown", {})


def test_akshare_source_capabilities():
    """测试 AkShareSourceAdapter 能力声明"""
    from data_layer.sources.akshare_source import AkShareSourceAdapter

    adapter = AkShareSourceAdapter()
    assert adapter.name == "akshare"
    assert "stock_list" in adapter.capabilities


def test_akshare_source_is_available():
    """测试 AkShareSourceAdapter 可用性检测"""
    from data_layer.sources.akshare_source import AkShareSourceAdapter

    adapter = AkShareSourceAdapter()
    # 有 akshare 库时应可用
    try:
        import akshare  # noqa: F401
        assert adapter.is_available() is True
    except ImportError:
        assert adapter.is_available() is False


def test_tushare_source_supports_historical_valuation():
    """tushare 必须声明 historical_valuation 能力, 供估值管道按当日全市场批量拉取。

    背景: ValuationPipeline.produces 需要的 capability 是 historical_valuation,
    baostock 唯一旧源不可达导致估值 0 覆盖。tushare.add historical_valuation 后
    管道可通过 get_daily_valuation 全市场批量拉当日估值。
    """
    from data_layer.sources.tushare_source import TushareSourceAdapter

    adapter = TushareSourceAdapter()
    assert "historical_valuation" in adapter.capabilities
    assert "kline" in adapter.capabilities
    # get_daily_valuation 是批量估值入口, 支持管道 legacy_fetch 回退路径
    assert callable(getattr(adapter, "get_daily_valuation", None))
    # get_data 需将 historical_valuation 路由到估值逻辑
    with patch.object(TushareSourceAdapter, "_get_pro") as mock_pro, \
         patch.object(TushareSourceAdapter, "_get_valuation") as mock_val:
        mock_val.return_value = {"code": "000001"}
        adapter.get_data("historical_valuation", {"code": "000001", "date": "2026-08-06"})
        mock_val.assert_called_once()


def _removed_test_akshare_source_get_data():
    """无 akshare 时 get_data 返回 None"""
    from data_layer.sources.akshare_source import AkShareSourceAdapter

    adapter = AkShareSourceAdapter()
    with patch.dict("sys.modules", {"akshare": None}):
        result = adapter.get_data("kline", {"code": "000001"})
        assert result is None






    # 无论旧系统是否真的存在，is_available 不应抛异常
    result = adapter.is_available()



    mock_inner = Mock()
    mock_inner.get_klines.return_value = []
    adapter._adapter = mock_inner

    result = adapter.get_data("kline", {
        "code": "000001", "start_date": "2025-01-01", "end_date": "2025-01-31"
    })
    assert result == []
    mock_inner.get_klines.assert_called_once_with("000001", "2025-01-01", "2025-01-31")



    assert adapter.get_data("unknown", {}) is None



    assert adapter._adapter is None
