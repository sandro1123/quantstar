"""DL-61②: tushare bulk 路径 turnover_rate 修复测试

根因: tushare get_daily_market 用 pro.daily() (不含 turnover_rate), 硬编码 0.0
→ daily_klines 全表 turnover_rate 恒 0, 打死 liquidity/turnover 类因子 (DL-66 普查)。
修复: 经 _get_daily_basic 共享缓存拉 daily_basic 按 code 合并 turnover_rate。

验收点:
1. get_daily_market 返回的 turnover_rate 来自 daily_basic (非 0)。
2. 共享缓存: get_daily_market + get_daily_valuation 同日只调一次 daily_basic。
3. daily_basic 失败 → turnover_rate 降级 0.0, 但 K 线主数据仍正常返回。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from data_layer.sources.tushare_source import TushareSourceAdapter


def _df(rows, cols):
    """构造一个最小 DataFrame 替身 (iterrows + empty)。"""
    class _Row:
        def __init__(self, d):
            self._d = d
        def get(self, k, default=None):
            return self._d.get(k, default)
        def __contains__(self, k):
            return k in self._d
    class _DF:
        def __init__(self, rows):
            self._rows = [_Row(r) for r in rows]
            self.empty = not self._rows
        def iterrows(self):
            for i, r in enumerate(self._rows):
                yield i, r
    return _DF(rows)


def _make_adapter(daily_rows, basic_rows):
    ad = TushareSourceAdapter()
    pro = MagicMock()
    pro.daily.return_value = _df(daily_rows, None)
    pro.daily_basic.return_value = _df(basic_rows, None)
    ad._pro = pro
    return ad, pro


DAILY = [{"ts_code": "600519.SH", "trade_date": "20260821", "open": 1500.0,
          "high": 1520.0, "low": 1490.0, "close": 1510.0, "vol": 30000.0,
          "amount": 4.5e7, "pre_close": 1500.0, "pct_chg": 0.67}]
BASIC = [{"ts_code": "600519.SH", "turnover_rate": 0.24, "pe_ttm": 30.0,
          "pb": 9.0, "ps_ttm": 12.0, "total_mv": 1.9e8, "total_share": 1.26e5,
          "dv_ttm": 1.5}]


def test_bulk_merges_turnover_rate_from_daily_basic():
    """DL-61②: get_daily_market 的 turnover_rate 来自 daily_basic, 非硬编码 0。"""
    ad, _ = _make_adapter(DAILY, BASIC)
    records = ad.get_daily_market("2026-08-21")
    assert len(records) == 1
    assert records[0]["turnover_rate"] == pytest.approx(0.24), \
        "turnover_rate 仍为 0 — daily_basic 合并未生效"
    assert records[0]["close"] == pytest.approx(1510.0)


def test_shared_cache_single_daily_basic_call():
    """DL-61②: kline bulk + valuation 同日共用一次 daily_basic 调用 (不加倍限频)。"""
    ad, pro = _make_adapter(DAILY, BASIC)
    ad.get_daily_market("2026-08-21")
    ad.get_daily_valuation("2026-08-21")
    assert pro.daily_basic.call_count == 1, \
        f"daily_basic 被调 {pro.daily_basic.call_count} 次, 应为 1 (共享缓存失效)"


def test_daily_basic_failure_degrades_to_zero():
    """DL-61②: daily_basic 挂掉 → turnover_rate 降级 0.0, K 线主数据仍返回。"""
    ad, pro = _make_adapter(DAILY, BASIC)
    pro.daily_basic.side_effect = RuntimeError("tushare rate limit")
    records = ad.get_daily_market("2026-08-21")
    assert len(records) == 1, "daily_basic 失败不应阻断 K 线写入"
    assert records[0]["turnover_rate"] == 0.0
    assert records[0]["close"] == pytest.approx(1510.0)


def test_code_not_in_daily_basic_gets_zero():
    """某股不在 daily_basic (如新股) → turnover_rate 0.0, 不 KeyError。"""
    ad, _ = _make_adapter(DAILY, [])  # daily_basic 空
    records = ad.get_daily_market("2026-08-21")
    assert records[0]["turnover_rate"] == 0.0
