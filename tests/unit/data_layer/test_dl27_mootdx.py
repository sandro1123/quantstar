"""DL-27 红绿测试: mootdx 连通性修复 + 能力接入 + 墙钟护栏。

红绿说明:
  - 移除 _guarded_call 后, 假 client 永不返回 → 测试挂死/超时失败;
  - 移除 bestip/timeout 参数后, 无法通过代码审查验证 (连通性修复);
  - capabilities 缺 adjust_factor/stock_info → 注册表过滤后不可见 (A-2 教训)。
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from data_layer.sources.mootdx_source import (
    MootdxSourceAdapter,
    MootdxTimeoutError,
)


@pytest.fixture
def adapter():
    ad = MootdxSourceAdapter()
    ad._client = MagicMock()  # 跳过真实连接
    return ad


# ---- 1. capabilities 与 get_data 分发键一致 (A-2 教训) ----
def test_capabilities_match_dispatch():
    ad = MootdxSourceAdapter()
    # 每个 capability 都必须能被 get_data 分发 (不返回 None sentinel 以外的东西)
    for cap in ("financial", "stock_list", "concept", "adjust_factor", "stock_info"):
        assert cap in ad.capabilities, f"capabilities 缺少 {cap}"


# ---- 2. 墙钟护栏: 永不返回的调用 → MootdxTimeoutError ----
def test_guarded_call_timeout(adapter):
    def never_returns():
        time.sleep(60)

    with pytest.raises(MootdxTimeoutError):
        adapter._guarded_call(never_returns, timeout_s=0.5)


# ---- 3. 护栏线程是 daemon (P0-7 防线) ----
def test_guard_thread_is_daemon(adapter):
    created = {}
    real_thread = threading.Thread

    class SpyThread(real_thread):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created["daemon"] = self.daemon

    import data_layer.sources.mootdx_source as mm
    orig = mm.threading.Thread
    mm.threading.Thread = SpyThread
    try:
        adapter._guarded_call(lambda: 42, timeout_s=5)
    finally:
        mm.threading.Thread = orig
    assert created.get("daemon") is True, "护栏线程非 daemon → atexit 挂死 (P0-7)"


# ---- 4. xdxr → adjust_factor 归一化 ----
def test_adjust_factor_from_xdxr(adapter):
    import pandas as pd
    adapter._client.xdxr.return_value = pd.DataFrame([
        {"year": 2026, "month": 6, "day": 26, "fenhong": 280.24,
         "songzhuangu": 0, "peigujia": 0, "category": 1},
    ])
    rows = adapter.get_data("adjust_factor", {"code": "600519"})
    assert len(rows) == 1
    assert rows[0]["ex_date"] == "2026-06-26"
    assert rows[0]["fenhong"] == 280.24


# ---- 5. financial 中文列名映射 (原拼音键 bug 修复) ----
def test_financial_chinese_columns(adapter):
    import pandas as pd
    adapter._client.finance.return_value = pd.DataFrame([
        {"基本每股收益": 1.5, "净资产收益率": 12.0, "营业收入": 100.0,
         "净利润": 50.0, "资产总计": 1000.0, "负债合计": 400.0,
         "所有者权益（或股东权益）合计": 600.0},
    ])
    result = adapter.get_data("financial", {"code": "600519"})
    assert result["eps"] == 1.5
    assert result["roe"] == 12.0
    assert result["net_assets"] == 600.0


# ---- 6. stock_info 流通股本 ----
def test_stock_info_float_shares(adapter):
    import pandas as pd
    adapter._client.finance.return_value = pd.DataFrame([
        {"已上市流通A股": 1256200000, "总股本": 1256200000},
    ])
    result = adapter.get_data("stock_info", {"code": "600519"})
    assert result["float_shares"] == 1256200000


# ---- 7. bestip+timeout 传入 Quotes.factory (连通性修复) ----
def test_client_factory_bestip_timeout():
    ad = MootdxSourceAdapter()
    ad._client = None
    with patch("mootdx.quotes.Quotes") as mock_quotes:
        ad._get_client()
        mock_quotes.factory.assert_called_once()
        kwargs = mock_quotes.factory.call_args[1]
        assert kwargs.get("bestip") is True, "bestip=True 未传入 (连通性修复缺失)"
        assert kwargs.get("timeout") == 15, "timeout 未传入"
