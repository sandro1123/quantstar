"""ddp 两小卡① — tushare bulk 路径重试+退避+诚实残留

根因: get_daily_market/_get_daily_basic 裸调 pro.daily()/daily_basic(),
网络抖动/限频一次即失败 → 回退日 bulk 路径静默吞错 (0980e30 ddp 预算
修复遗留卡)。修复: _retry_call 统一重试+指数退避, 可重试异常分类,
穷尽后上抛交 kline 管道 except 落 pipeline_runs (诚实残留, 宁 NULL 不假值)。
"""
import pytest

from data_layer.sources.tushare_source import TushareSourceAdapter


class _FakeDF:
    """pandas DataFrame 最小替身 — 只需 empty 属性与 iterrows。"""

    def __init__(self, empty=False):
        self.empty = empty

    def iterrows(self):
        return iter([])


class _ScriptedPro:
    """按脚本依次返回/抛错的假 pro 客户端。"""

    def __init__(self, script):
        self.script = list(script)  # 每项: DataFrame 值 / Exception 实例
        self.calls = {"daily": 0, "daily_basic": 0}

    def daily(self, trade_date=""):
        self.calls["daily"] += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def daily_basic(self, trade_date=""):
        self.calls["daily_basic"] += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _adapter_with(pro, monkeypatch):
    ad = TushareSourceAdapter()
    monkeypatch.setattr(ad, "_get_pro", lambda: pro)
    monkeypatch.setattr(TushareSourceAdapter, "BULK_BACKOFF_BASE_S", 0.0,
                        raising=False)  # 测试零退避
    return ad


class TestRetryableClassification:
    def test_network_timeout_retryable(self):
        assert TushareSourceAdapter._is_retryable(
            ConnectionError("Read timed out")) is True

    def test_rate_limit_retryable(self):
        assert TushareSourceAdapter._is_retryable(
            Exception("每分钟最多访问该接口1次")) is True

    def test_param_error_not_retryable(self):
        assert TushareSourceAdapter._is_retryable(
            ValueError("bad param: ts_code invalid")) is False

    def test_auth_error_not_retryable(self):
        assert TushareSourceAdapter._is_retryable(
            Exception("token invalid, 请检查token")) is False


class TestGetDailyMarketRetry:
    def test_retry_then_success(self, monkeypatch):
        """可重试异常 → 重试后成功, 返回值不被修饰"""
        pro = _ScriptedPro([
            TimeoutError("connection timed out"), _FakeDF(empty=False)])
        ad = _adapter_with(pro, monkeypatch)
        recs = ad.get_daily_market("2026-08-28")
        assert recs == []          # 空 DF 无 iterrows 行 → [], 但调用成功
        assert pro.calls["daily"] == 2  # 1 失败 + 1 重试成功

    def test_exhausted_raises(self, monkeypatch):
        """穷尽重试 → 上抛 (kline 管道 except 收 errors 落 pipeline_runs)"""
        pro = _ScriptedPro([
            TimeoutError("timeout"), TimeoutError("timeout"),
            TimeoutError("timeout")])
        ad = _adapter_with(pro, monkeypatch)
        with pytest.raises(TimeoutError):
            ad.get_daily_market("2026-08-28")
        assert pro.calls["daily"] == 3  # BULK_MAX_ATTEMPTS=3 封顶

    def test_non_retryable_fails_fast(self, monkeypatch):
        """不可重试 (参数错) → 不重试立即上抛"""
        pro = _ScriptedPro([ValueError("bad param")])
        ad = _adapter_with(pro, monkeypatch)
        with pytest.raises(ValueError):
            ad.get_daily_market("2026-08-28")
        assert pro.calls["daily"] == 1  # 零重试

    def test_empty_df_honest_empty(self, monkeypatch):
        """空表返回 [] 不冒充成功 — _BULK_MIN_ROWS 阈值拒收, 原语义保留"""
        pro = _ScriptedPro([_FakeDF(empty=True)])
        ad = _adapter_with(pro, monkeypatch)
        assert ad.get_daily_market("2026-08-28") == []
        assert pro.calls["daily"] == 1


class TestDailyBasicRetry:
    def test_retry_then_cache_none_on_exhaust(self, monkeypatch):
        """daily_basic 穷尽重试 → 缓存 None 降级语义不变, 但痕迹有日志"""
        pro = _ScriptedPro([
            Exception("server error 503"),
            Exception("server error 503"),
            Exception("server error 503")])
        ad = _adapter_with(pro, monkeypatch)
        assert ad._get_daily_basic("20260828") is None
        assert pro.calls["daily_basic"] == 3
        # 失败也进缓存 — 同日不再重复打已故障接口 (DL-61② 原语义)
        assert ad._get_daily_basic("20260828") is None
        assert pro.calls["daily_basic"] == 3

    def test_daily_basic_success_after_retry(self, monkeypatch):
        pro = _ScriptedPro([
            Exception("network unreachable"), _FakeDF(empty=False)])
        ad = _adapter_with(pro, monkeypatch)
        dfb = ad._get_daily_basic("20260828")
        assert dfb is not None and dfb.empty is False
        assert pro.calls["daily_basic"] == 2

    def test_daily_basic_non_retryable_cached_none(self, monkeypatch):
        """鉴权错不重试 → 直接缓存 None"""
        pro = _ScriptedPro([Exception("token invalid")])
        ad = _adapter_with(pro, monkeypatch)
        assert ad._get_daily_basic("20260828") is None
        assert pro.calls["daily_basic"] == 1


class TestBackoffBehavior:
    def test_backoff_seconds_exponential(self, monkeypatch):
        """退避 2s/4s 指数翻倍 — 用真实退避常量验证 sleep 调用序列"""
        sleeps = []
        monkeypatch.setattr("data_layer.sources.tushare_source.time.sleep",
                            lambda s: sleeps.append(s))
        pro = _ScriptedPro([
            TimeoutError("timeout"), TimeoutError("timeout"),
            TimeoutError("timeout")])
        ad = TushareSourceAdapter()
        monkeypatch.setattr(ad, "_get_pro", lambda: pro)
        with pytest.raises(TimeoutError):
            ad.get_daily_market("2026-08-28")
        assert sleeps == [2.0, 4.0]  # 3 次尝试只睡 2 次
        assert pro.calls["daily"] == 3
