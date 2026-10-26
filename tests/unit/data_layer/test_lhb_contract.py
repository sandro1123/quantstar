"""卡② 回归测试: lhb 契约对齐 — report_date 显式传递 + adata 字段映射 + 不静默降级。

病 (2026-09-15 生产实爆, 09-16 00:0x 取证):
  09-15 16:02 lhb 腿 rc=84 success 写库, 但 date='2026-09-15' 的 **72 行三金额列全零**,
  rowid 块 2134~2205; 与 adata 09-14 的 72 码 100% 重合 (T-1 铁证)。
  链: fuyao 熔断 (15:47:19/16:01:10 两次开 300s) → 首源变 adata
      → lhb.py **从不传 report_date** → adata 缺省回退 T-1(09-14)
      → 字段错位 (adata 返 net_amount/buy_amount/sell_amount, handler 读
        buy_in/turnover/capitalization) → 全零金额
      → handler 用 input.date(09-15) 落库
  后果重于脏数据: PK(code,date) + INSERT OR IGNORE → 09-15 的 72 个正确行永久进不来。

实测前置 (09-16 侦察): adata.list_a_list_daily(report_date=D) 返回行的 trade_date
恰为 D (09-08/10/11/14/15 五日全一致) — T-1 回退不仅错, 且完全没必要。
"""
import sqlite3

import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.lhb import LHBPipeline
from data_layer.sources.adata_source import ADataSourceAdapter


class _FakeDB:
    def __init__(self, conn): self._conn = conn
    def get_connection(self): return self._conn


class _Reg:
    """最小 registry: 只实现 lhb 腿用到的 get_sources_for。"""

    def __init__(self, sources): self._sources = sources

    def get_sources_for(self, data_type):
        return [s for s in self._sources if data_type in s.capabilities]


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE lhb_daily (code TEXT, date TEXT, net_buy_amount REAL, "
                 "total_buy REAL, total_sell REAL, PRIMARY KEY (code, date))")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# (a) lhb.py 显式传 report_date — 禁缺省回退 T-1
# ---------------------------------------------------------------------------

class _RecordingSource:
    """记录收到的 params, 返回契约字段行。"""

    def __init__(self, name="rec", rows=None):
        self.name = name
        self._rows = rows if rows is not None else []
        self.seen_params = []

    @property
    def capabilities(self): return {"lhb"}
    def is_available(self): return True

    def get_data(self, data_type, params):
        self.seen_params.append(dict(params))
        return self._rows


def test_pipeline_passes_report_date_to_sources(monkeypatch):
    """lhb 腿必须把 report_date 显式传给源 (缺省回退 T-1 是全零事故的直接因)。"""
    src = _RecordingSource(rows=[{"code": "600001", "buy_in": 1.0,
                                  "turnover": 2.0, "capitalization": 3.0}])
    pipe = LHBPipeline(db=None, source_registry=_Reg([src]), fusion=None)
    conn = _make_db()
    pipe.run(PipelineInput(date="2026-09-15", db=_FakeDB(conn)))

    assert src.seen_params, "源应被调用"
    p = src.seen_params[0]
    assert p.get("report_date") == "2026-09-15", (
        f"必须显式传 report_date=腿日期, 实际 params={p!r}"
    )


def test_pipeline_fusion_path_passes_report_date(monkeypatch):
    """走 fusion 路径时 report_date 同样必须传入 (生产实际走这条)。"""
    seen = {}

    class _Fusion:
        def fuse(self, data_type, params, key_field=None):
            seen.update(params)
            return ([{"code": "600001", "buy_in": 1.0, "turnover": 2.0,
                      "capitalization": 3.0}], None)

    pipe = LHBPipeline(db=None, source_registry=None, fusion=_Fusion())
    conn = _make_db()
    pipe.run(PipelineInput(date="2026-09-15", db=_FakeDB(conn)))

    assert seen.get("report_date") == "2026-09-15", (
        f"fusion 路径必须带 report_date, 实际 params={seen!r}"
    )


# ---------------------------------------------------------------------------
# (b) adata 字段映射与落库列对齐
# ---------------------------------------------------------------------------

class _Row(dict):
    def get(self, k, d=None): return dict.get(self, k, d)


def _df(rows):
    from unittest.mock import MagicMock
    df = MagicMock()
    df.empty = False
    df.iterrows.return_value = [(i, _Row(r)) for i, r in enumerate(rows)]
    return df


def test_adata_lhb_maps_to_pipeline_contract_fields():
    """adata 必须返 lhb 腿读的契约字段 buy_in/turnover/capitalization。

    旧映射返 net_amount/buy_amount/sell_amount → handler 读不到 → 金额全零 (09-15 事故)。
    """
    a = ADataSourceAdapter()
    a._init_ok = True
    from unittest.mock import MagicMock
    hot = MagicMock()
    hot.list_a_list_daily.return_value = _df([
        {"stock_code": "000020", "trade_date": "2026-09-15", "close": 14.9,
         "change_cpt": 8.7591, "a_net_amount": 10875600.0,
         "a_buy_amount": 90645560.0, "a_sell_amount": 79769960.0,
         "reason": "涨幅偏离值20%"}])
    a._hot = hot
    out = a.get_data("lhb", {"report_date": "2026-09-15"})

    assert len(out) == 1
    r = out[0]
    assert r["code"] == "000020"
    assert r["buy_in"] == pytest.approx(10875600.0), "净买 → buy_in (handler 读这个键)"
    assert r["turnover"] == pytest.approx(90645560.0), "总买 → turnover"
    assert r["capitalization"] == pytest.approx(79769960.0), "总卖 → capitalization"


def test_adata_lhb_row_survives_pipeline_write_with_nonzero_amounts():
    """端到端: adata 行经 lhb 腿落库后金额非零 (全零事故的回归锚)。"""
    a = ADataSourceAdapter()
    a._init_ok = True
    from unittest.mock import MagicMock
    hot = MagicMock()
    hot.list_a_list_daily.return_value = _df([
        {"stock_code": "000020", "trade_date": "2026-09-15",
         "a_net_amount": 10875600.0, "a_buy_amount": 90645560.0,
         "a_sell_amount": 79769960.0}])
    a._hot = hot

    class _Fusion:
        def fuse(self, data_type, params, key_field=None):
            return (a.get_data("lhb", params), None)

    conn = _make_db()
    pipe = LHBPipeline(db=None, source_registry=None, fusion=_Fusion())
    res = pipe.run(PipelineInput(date="2026-09-15", db=_FakeDB(conn)))

    assert res.records_written == 1
    row = conn.execute("SELECT net_buy_amount, total_buy, total_sell "
                       "FROM lhb_daily WHERE code='000020'").fetchone()
    assert row[0] == pytest.approx(10875600.0), f"净买不得为 0 (实际 {row[0]})"
    assert row[1] == pytest.approx(90645560.0)
    assert row[2] == pytest.approx(79769960.0)


# ---------------------------------------------------------------------------
# (c) report_date 不可得 → 必须报错, 不许静默回退 T-1 (铁律 #12)
# ---------------------------------------------------------------------------

def test_adata_lhb_missing_report_date_raises_not_silent_fallback():
    """adata 收不到 report_date → 必须报错 (禁静默回退 T-1)。

    旧行为: 缺省回退昨天 → 拉 T-1 数据以当日日期落库 = 09-15 全零事故的成因。
    """
    a = ADataSourceAdapter()
    a._init_ok = True
    from unittest.mock import MagicMock
    hot = MagicMock()
    a._hot = hot

    with pytest.raises(Exception) as ei:
        a.get_data("lhb", {})
    assert "report_date" in str(ei.value).lower(), (
        f"报错信息须含 report_date 指向病因, 实际: {ei.value}"
    )
    hot.list_a_list_daily.assert_not_called(), "不得在缺 report_date 时仍去打源"


def test_pipeline_failure_reason_names_report_date_and_source():
    """腿失败时 errors 必须写明「report_date 不可得(源: ...)」— 指向病因非症状。

    统筹裁定 (sm-13caf48691a54065): failed 的 reason 必须写明
    「report_date 不可得(源: fuyao/adata 熔断)」, 否则运维要去猜是哪个源。
    """
    class _FusionRaises:
        def fuse(self, data_type, params, key_field=None):
            raise RuntimeError("report_date 不可得: 源 fuyao/adata 熔断, zzshare 无数据")

    conn = _make_db()
    pipe = LHBPipeline(db=None, source_registry=None, fusion=_FusionRaises())
    res = pipe.run(PipelineInput(date="2026-09-15", db=_FakeDB(conn)))

    assert res.status == "failed", f"源契约不可得应为 failed 而非 {res.status}"
    joined = " ".join(res.errors)
    assert "report_date" in joined, f"失败信息须指向 report_date, 实际 {res.errors!r}"
    assert res.records_written == 0, "failed 时不得写入任何行 (尤其全零占位行)"
