"""DL-25 回归测试: baostock 恢复接入 + 批量 K 线注册表可见能力 + 17:30 时间闸。

覆盖:
1. baostock capabilities 声明 kline_bulk/kline (否则 type_priorities 是死值)
2. SourceRegistry 按 type_priority 排序 kline_bulk: tushare(1) 主 → baostock(2) 备
3. 时间闸: 当日早于 17:30 跳过 baostock; 历史日期恒就绪
4. 主源失败 + baostock 就绪 → 自动降级且行数达标
"""
from datetime import datetime

import pytest

from data_layer.pipelines.kline import KlinePipeline, _baostock_bulk_ready
from data_layer.pipelines.base import PipelineInput
from data_layer._models import Kline
from data_layer.sources.baostock_source import BaoStockSourceAdapter
from data_layer.sources.registry import SourceRegistry


# ---- 1. capabilities 声明 ----
def test_baostock_declares_bulk_and_kline():
    caps = BaoStockSourceAdapter.capabilities
    assert "kline_bulk" in caps, "kline_bulk 未声明 → 注册表无法路由到 baostock"
    assert "kline" in caps
    assert "adjust_factor" in caps
    assert "dividend" in caps


# ---- 2. 注册表排序 ----
class _Stub:
    def __init__(self, name, caps): self.name = name; self.capabilities = caps
    def is_available(self): return True


def test_registry_kline_bulk_order_tushare_first():
    reg = SourceRegistry()
    reg.register(_Stub("tushare", {"kline_bulk"}), priority=20,
                 type_priorities={"kline_bulk": 1})
    reg.register(_Stub("baostock", {"kline_bulk"}), priority=40,
                 type_priorities={"kline_bulk": 2})
    names = [s.name for s in reg.get_sources_for("kline_bulk")]
    assert names == ["tushare", "baostock"], names


def test_registry_excludes_source_without_capability():
    """无 kline_bulk 能力的源 (如 akshare_sina) 不得出现在 bulk 路径。"""
    reg = SourceRegistry()
    reg.register(_Stub("tushare", {"kline_bulk"}), priority=20,
                 type_priorities={"kline_bulk": 1})
    reg.register(_Stub("akshare_sina", {"kline"}), priority=2)  # 只有逐股 kline
    names = [s.name for s in reg.get_sources_for("kline_bulk")]
    assert names == ["tushare"], names


# ---- 3. 时间闸 ----
def test_baostock_ready_for_historical_date():
    assert _baostock_bulk_ready("2026-08-14") is True


def test_baostock_gate_today(monkeypatch):
    """当日: 早于 17:30 未就绪, 晚于 17:30 就绪。"""
    from datetime import date as _date
    today = _date.today().isoformat()

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 22, 15, 30)
    monkeypatch.setattr("data_layer.pipelines.kline._dt", FakeDT)
    assert _baostock_bulk_ready(today) is False

    class FakeDT2(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 22, 17, 31)
    monkeypatch.setattr("data_layer.pipelines.kline._dt", FakeDT2)
    assert _baostock_bulk_ready(today) is True


# ---- 4. 批量降级 ----
class FakeSource:
    def __init__(self, name, bulk=None): self._name = name; self._bulk = bulk
    @property
    def name(self): return self._name
    def get_daily_market(self, d): return self._bulk(d) if self._bulk else []


class FakeRepo:
    def __init__(self): self.upserted = []
    class _K:
        def __init__(self, outer): self.outer = outer
        def batch_upsert(self, recs): self.outer.upserted.extend(recs)
        def atomic_upsert_date(self, date, recs, min_rows=0):
            if min_rows and len(recs) < min_rows:
                return -1
            self.outer.upserted.extend(recs)
            return len(recs)
    @property
    def kline(self): return self._K(self)
    class _S:
        def get_all_active(self): return ["600001", "000001"]
    @property
    def stock(self): return self._S()


class FakeReg:
    def __init__(self, sources): self._s = sources
    def get_sources_for(self, t): return self._s


def _klines(n, date="2026-08-14"):
    return [Kline(code=f"{600000+i:06d}", date=date, open=1, high=1, low=1,
                 close=1, volume=1, amount=1, change_pct=0.0) for i in range(n)]


def test_bulk_fallback_to_baostock_when_tushare_fails(monkeypatch):
    """主源 tushare 返回空 → 自动降级 baostock (历史日期, 时间闸放行)。"""
    tushare = FakeSource("tushare", bulk=lambda d: [])
    baostock = FakeSource("baostock", bulk=lambda d: _klines(5211))
    reg = FakeReg([tushare, baostock])
    pl = KlinePipeline(source_registry=reg)
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.kline.get_repo", lambda db: repo)
    res = pl.run(PipelineInput(date="2026-08-14", start_date="2026-08-14",
                               end_date="2026-08-14", db=None))
    assert res.status == "success", res
    assert res.records_written == 5211
    assert res.metadata.get("source") == "baostock"


def test_bulk_skips_baostock_today_before_1730(monkeypatch):
    """当日早于 17:30: baostock 被时间闸跳过, 不空跑。"""
    from datetime import date as _date
    today = _date.today().isoformat()

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 22, 15, 30)
    monkeypatch.setattr("data_layer.pipelines.kline._dt", FakeDT)

    called = {"baostock": 0}
    def bao_bulk(d):
        called["baostock"] += 1
        return _klines(5211, date=today)
    tushare = FakeSource("tushare", bulk=lambda d: [])
    baostock = FakeSource("baostock", bulk=bao_bulk)
    reg = FakeReg([tushare, baostock])
    pl = KlinePipeline(source_registry=reg)
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.kline.get_repo", lambda db: repo)
    pl.run(PipelineInput(date=today, start_date=today, end_date=today, db=None))
    assert called["baostock"] == 0, "早于 17:30 不应调用 baostock 批量"
