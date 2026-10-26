"""回归测试: kline 批量路径最低覆盖阈值。

修复前: 批量源只要返回非空(哪怕仅 100 只)即提前 return success, 掩盖残缺抓取。
修复后: 批量结果必须 >= 4000 行才接受, 否则继续尝试下一个源 / 回落逐股。
"""
import pytest
from data_layer.pipelines.kline import KlinePipeline
from data_layer.pipelines.base import PipelineInput
from data_layer._models import Kline


class FakeSource:
    def __init__(self, name, bulk=None): self._name=name; self._bulk=bulk
    @property
    def name(self): return self._name
    def get_daily_market(self, d): return self._bulk(d) if self._bulk else []


class FakeRepo:
    def __init__(self): self.upserted=[]
    class _K:
        def __init__(self, outer): self.outer=outer
        def batch_upsert(self, recs): self.outer.upserted.extend(recs)
        def atomic_upsert_date(self, date, recs, min_rows=0):
            # DL-03: 模拟原子写入 — 达标则入库并返回行数, 不达标返回 -1
            if min_rows and len(recs) < min_rows:
                return -1
            self.outer.upserted.extend(recs)
            return len(recs)
    @property
    def kline(self): return self._K(self)
    class _S:
        def get_all_active(self): return ["600001","000001"]  # 非空, 模拟真实 stock_info
    @property
    def stock(self): return self._S()


class FakeReg:
    def __init__(self, sources): self._s=sources
    def get_sources_for(self, t): return self._s


def _klines(n, date="2026-08-14"):
    return [Kline(code=f"{600000+i:06d}", date=date, open=1, high=1, low=1,
                 close=1, volume=1, amount=1, change_pct=0.0) for i in range(n)]


def test_bulk_rejects_partial_accepts_full(monkeypatch):
    """源A返回100(残缺)应被跳过, 源B返回5540应被接受。"""
    srcA=FakeSource("A", bulk=lambda d: _klines(100))
    srcB=FakeSource("B", bulk=lambda d: _klines(5540))
    reg=FakeReg([srcA, srcB])
    pl=KlinePipeline(source_registry=reg)
    repo=FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.kline.get_repo", lambda db: repo)
    res=pl.run(PipelineInput(date="2026-08-14", start_date="2026-08-14", end_date="2026-08-14", db=None))
    assert res.status=="success", res
    assert res.records_written==5540
    assert len(repo.upserted)==5540  # only full batch from B upserted, not A's 100


def test_bulk_all_partial_falls_to_per_stock(monkeypatch):
    """所有批量源都残缺(<4000)时, 不应批量入库, 应回落逐股(此处逐股无源->0行+errors)。"""
    src=FakeSource("A", bulk=lambda d: _klines(100))
    reg=FakeReg([src])
    pl=KlinePipeline(source_registry=reg)
    repo=FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.kline.get_repo", lambda db: repo)
    res=pl.run(PipelineInput(date="2026-08-14", start_date="2026-08-14", end_date="2026-08-14", db=None))
    # 残缺批量不入库; 逐股路径会跑(但 FakeSource 无 get_data -> errors), status partial/failed
    assert len(repo.upserted)==0, f"残缺批量不应入库, got {len(repo.upserted)}"
