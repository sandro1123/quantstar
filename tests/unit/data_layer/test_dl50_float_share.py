"""DL-50: 流通股本源接入 — tushare float_share capability

测试覆盖:
1. tushare get_float_shares 正常返回 (mock daily_basic)
2. tushare get_float_shares 空数据/错误 → 空 dict
3. tushare get_float_shares 墙钟超时 → 空 dict
4. get_daily_valuation 记录包含 float_share 字段
5. DataLayer.get_float_shares 读 API (registry 路由)
6. capabilities 声明包含 float_share

:memory: 库, 无生产依赖。
"""

import threading
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# === tushare_source 单元测试 ===

class TestTushareFloatShares:
    def _mk_adapter(self):
        from data_layer.sources.tushare_source import TushareSourceAdapter
        adapter = TushareSourceAdapter()
        return adapter

    def test_get_float_shares_normal(self):
        """正常返回: mock daily_basic DataFrame"""
        adapter = self._mk_adapter()
        df = pd.DataFrame({
            "ts_code": ["000001.SZ", "600519.SH", "300750.SZ"],
            "float_share": [100.0, 50.0, 200.0],  # 万股
        })
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = df
        adapter._pro = mock_pro

        result = adapter.get_float_shares("2026-08-21")
        assert result == {
            "000001": 100.0 * 10000,
            "600519": 50.0 * 10000,
            "300750": 200.0 * 10000,
        }
        mock_pro.daily_basic.assert_called_once_with(trade_date="20260821")

    def test_get_float_shares_empty_df(self):
        """空 DataFrame → 空 dict"""
        adapter = self._mk_adapter()
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = pd.DataFrame()
        adapter._pro = mock_pro

        result = adapter.get_float_shares("2026-08-21")
        assert result == {}

    def test_get_float_shares_none_df(self):
        """daily_basic 返回 None → 空 dict"""
        adapter = self._mk_adapter()
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = None
        adapter._pro = mock_pro

        result = adapter.get_float_shares("2026-08-21")
        assert result == {}

    def test_get_float_shares_exception(self):
        """daily_basic 抛异常 → 空 dict"""
        adapter = self._mk_adapter()
        mock_pro = MagicMock()
        mock_pro.daily_basic.side_effect = ConnectionError("network")
        adapter._pro = mock_pro

        result = adapter.get_float_shares("2026-08-21")
        assert result == {}

    def test_get_float_shares_no_pro(self):
        """无 pro (无 token) → 空 dict"""
        adapter = self._mk_adapter()
        adapter._pro = None
        with patch.object(adapter, "_get_pro", return_value=None):
            result = adapter.get_float_shares("2026-08-21")
        assert result == {}

    def test_get_float_shares_zero_excluded(self):
        """float_share=0 的行不进入结果"""
        adapter = self._mk_adapter()
        df = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "float_share": [100.0, 0.0],
        })
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = df
        adapter._pro = mock_pro

        result = adapter.get_float_shares("2026-08-21")
        assert "000001" in result
        assert "000002" not in result

    def test_get_float_shares_timeout(self):
        """墙钟超时 → 空 dict (不挂死)"""
        adapter = self._mk_adapter()
        mock_pro = MagicMock()

        def slow_call(**kwargs):
            threading.Event().wait(120)  # 模拟挂死
            return pd.DataFrame()

        mock_pro.daily_basic.side_effect = slow_call
        adapter._pro = mock_pro

        # patch join timeout to 0.1s for test speed
        with patch("threading.Thread.join", return_value=None):
            with patch("threading.Thread.is_alive", return_value=True):
                result = adapter.get_float_shares("2026-08-21")
        assert result == {}

    def test_capabilities_include_float_share(self):
        """capabilities 声明包含 float_share"""
        from data_layer.sources.tushare_source import TushareSourceAdapter
        assert "float_share" in TushareSourceAdapter.capabilities

    def test_get_data_dispatch_float_share(self):
        """get_data('float_share', ...) 路由到 get_float_shares"""
        adapter = self._mk_adapter()
        df = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "float_share": [100.0],
        })
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = df
        adapter._pro = mock_pro

        result = adapter.get_data("float_share", {"date": "2026-08-21"})
        assert result == {"000001": 1000000.0}


# === get_daily_valuation 包含 float_share ===

class TestDailyValuationFloatShare:
    def test_valuation_records_include_float_share(self):
        """get_daily_valuation 记录包含 float_share 字段"""
        from data_layer.sources.tushare_source import TushareSourceAdapter
        adapter = TushareSourceAdapter()
        df = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "pe_ttm": [10.0],
            "pb": [1.5],
            "ps_ttm": [2.0],
            "total_mv": [1000.0],
            "total_share": [500.0],
            "dv_ttm": [3.0],
            "turnover_rate": [1.2],
            "float_share": [300.0],  # 万股
        })
        mock_pro = MagicMock()
        mock_pro.daily_basic.return_value = df
        adapter._pro = mock_pro

        records = adapter.get_daily_valuation("2026-08-21")
        assert len(records) == 1
        assert records[0]["float_share"] == 300.0 * 10000  # 万股→股


# === DataLayer.get_float_shares 读 API ===

class TestDataLayerFloatShares:
    def _mk_dl(self, mock_registry):
        """构造带 mock _source_registry 的 DataLayer (跳过 __init__)"""
        from data_layer import DataLayer
        dl = DataLayer.__new__(DataLayer)
        dl.db = MagicMock()
        dl._source_registry = mock_registry
        return dl

    def test_get_float_shares_via_registry(self):
        """DataLayer.get_float_shares 通过 _source_registry 路由到 tushare"""
        mock_source = MagicMock()
        mock_source.get_data.return_value = {"000001": 1000000.0}

        mock_registry = MagicMock()
        mock_registry.get_sources_for.return_value = [mock_source]

        dl = self._mk_dl(mock_registry)
        result = dl.get_float_shares("2026-08-21")

        assert result == {"000001": 1000000.0}
        mock_registry.get_sources_for.assert_called_once_with("float_share")

    def test_get_float_shares_no_source_returns_empty(self):
        """无可用源 → 空 dict"""
        mock_registry = MagicMock()
        mock_registry.get_sources_for.return_value = []

        dl = self._mk_dl(mock_registry)
        result = dl.get_float_shares("2026-08-21")

        assert result == {}

    def test_get_float_shares_source_exception_returns_empty(self):
        """源抛异常 → 空 dict (诚实降级)"""
        mock_source = MagicMock()
        mock_source.get_data.side_effect = ConnectionError("down")

        mock_registry = MagicMock()
        mock_registry.get_sources_for.return_value = [mock_source]

        dl = self._mk_dl(mock_registry)
        result = dl.get_float_shares("2026-08-21")

        assert result == {}
