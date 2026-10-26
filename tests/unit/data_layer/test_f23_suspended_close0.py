"""TDD: F-23 停牌日 close=0 代码层两件 (不写库).

第①件 源层: 停牌日 close=0 的行不产出 Kline (与 baostock_source:416 的
              `if tradestatus != '1': continue` 对齐)。
第②件 管道层: close<=0 时 change_pct 记 NULL (不是 0, 也不是 -100),
              且 prev_close 不被 0 污染 (用户已裁: 0 会被下游读成"平盘",
              NULL 才是"不可算")。

6 钉:
  ①件-1 源返回 close=0 的行 → 不产出 Kline (最关键)
  ①件-2 源返回 close>0 的行 → 正常产出, 逐字段不变
  ①件-3 混批 (有 0 有正常) → 只跳 0 的, 正常的全在
  ②件-1 close=0 + prev_close>0 → change_pct is None (不是 -100, 不是 0)
  ②件-2 close=0 后 prev_close 不被更新为 0 (下一行还能正常算)
  ②件-3 close>0 的正常行 → change_pct 照常计算, 逐值不变

边界: easyquotation 的 `_to_kline` 是 staticmethod 且被既有测试
(test_judge186_amount_volume / test_eq_wire) 直接调用 → **不改其签名**,
只在 `_fetch_sina` 层过滤 (保留 _to_kline 纯映射语义)。
"""

from types import SimpleNamespace

from data_layer._models import Kline
from data_layer.sources.akshare_sina_source import AkshareSinaAdapter
from data_layer.sources.easyquotation_source import EasyQuotationSourceAdapter


# ══════════════ 第①件: 源层停牌日不写 0 ══════════════

def _fake_akshare_sina(rows):
    class _DF:
        def __init__(self, rows):
            self._rows = rows
            self.empty = False

        def iterrows(self):
            for i, r in enumerate(self._rows):
                yield i, r

    class _AK:
        def stock_zh_a_daily(self, **kwargs):
            return _DF(rows)

    return _AK()


def _akshare_sina_klines(rows, code="600001"):
    src = AkshareSinaAdapter.__new__(AkshareSinaAdapter)
    return src._get_klines(_fake_akshare_sina(rows),
                           {"code": code, "start_date": "2026-10-20",
                            "end_date": "2026-10-20"})


def test_source_skips_zero_close_row():
    """①件-1: 源返回 close=0 的停牌行 → 不产出 Kline (最关键)."""
    rows = [{"date": "2026-10-20", "open": 0, "high": 0, "low": 0,
             "close": 0, "volume": 0, "amount": 0}]
    out = _akshare_sina_klines(rows)
    assert out == [], f"停牌行 (close=0) 不得产出 Kline; 实得 {out}"


def test_source_keeps_normal_row_unchanged():
    """①件-2: close>0 的正常行 → 正常产出, 逐字段不变."""
    rows = [{"date": "2026-10-20", "open": 10.0, "high": 11.0, "low": 9.5,
             "close": 10.5, "volume": 1000.0, "amount": 10500.0,
             "pre_close": 10.0}]
    out = _akshare_sina_klines(rows)
    assert len(out) == 1, f"正常行必须产出; 实得 {out}"
    k = out[0]
    assert k.code == "600001"
    assert k.date == "2026-10-20"
    assert k.open == 10.0
    assert k.high == 11.0
    assert k.low == 9.5
    assert k.close == 10.5
    assert k.volume == 1000.0
    assert k.amount == 10500.0
    assert k.change_pct == 5.0, f"change_pct 应照常算; 实得 {k.change_pct}"


def test_source_mixed_batch_skips_only_zero():
    """①件-3: 混批 (有 0 有正常) → 只跳 0 的, 正常的全在."""
    rows = [
        {"date": "2026-10-20", "open": 10.0, "high": 11.0, "low": 9.5,
         "close": 10.5, "volume": 1000.0, "amount": 10500.0, "pre_close": 10.0},
        {"date": "2026-10-20", "open": 0, "high": 0, "low": 0,
         "close": 0, "volume": 0, "amount": 0, "pre_close": 0},
        {"date": "2026-10-20", "open": 20.0, "high": 21.0, "low": 19.5,
         "close": 20.5, "volume": 2000.0, "amount": 41000.0, "pre_close": 20.0},
    ]
    out = _akshare_sina_klines(rows)
    assert len(out) == 2, f"只跳 0 的, 正常两条全在; 实得 {len(out)}"
    assert sorted(k.close for k in out) == [10.5, 20.5], f"实得 closes={[k.close for k in out]}"


def _eq_fetch_sina(row):
    """用假的 easyquotation 走真实 _fetch_sina (只替换库入口)."""
    class _FakeQ:
        @staticmethod
        def use(which):
            class _Q:
                @staticmethod
                def real(symbols):
                    return {"600001": row}
            return _Q()

    import easyquotation
    orig = easyquotation.use
    easyquotation.use = _FakeQ.use
    try:
        src = EasyQuotationSourceAdapter.__new__(EasyQuotationSourceAdapter)
        return src._fetch_sina("600001")
    finally:
        easyquotation.use = orig


def test_easyquotation_fetch_skips_zero_now():
    """①件-1 扩展: easyquotation 停牌实时价 now=0 → _fetch_sina 不产出 Kline.

    easyquotation sina 停牌时 now=0 (无成交), 同样会落库成 0 价行.
    注意: `_to_kline` 签名不变 (既有测试直接调它), 过滤在 _fetch_sina 层.
    """
    row = {"now": 0, "open": 0, "high": 0, "low": 0, "close": 10.0,
           "turnover": 0, "volume": 0, "date": "2026-10-20"}
    out = _eq_fetch_sina(row)
    assert out == [], f"停牌 (now=0) 不得产出 Kline; 实得 {out}"


def test_easyquotation_fetch_keeps_normal_now():
    """①件-2 扩展: now>0 的正常行 → _fetch_sina 照常产出, 逐字段不变."""
    row = {"now": 10.5, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.0,
           "turnover": 1000, "volume": 10500, "date": "2026-10-20"}
    out = _eq_fetch_sina(row)
    assert len(out) == 1, f"正常行必须产出; 实得 {out}"
    k = out[0]
    assert k.close == 10.5
    assert k.pre_close == 10.0
    assert k.change_pct == 5.0, f"change_pct 应照常算; 实得 {k.change_pct}"
    assert k.volume == 1000, "量/额键名语义相反, 落库须对调 (判例 #186)"
    assert k.amount == 10500


# ══════════════ 第②件: 管道层 close<=0 防护 ══════════════

def _kline(date, close, pre_close=0.0):
    return Kline(code="600001", date=date, open=close, high=close, low=close,
                 close=close, volume=100.0, amount=1000.0,
                 pre_close=pre_close, change_pct=0.0)


def _repo_with_prev(prev_close_value):
    """构造 repo, 其 db 查库拿到的 prev_close = 指定值.

    _recompute_change_pct 取 db 的顺序:
      getattr(repo, 'db', None) → repo.get_connection() → repo.kline.db
    故 db 必须挂在 repo 上 (不是 pipeline 上)。
    """
    class _Conn:
        def execute(self, sql, args):
            class _Cur:
                def fetchone(self):
                    return (prev_close_value,) if prev_close_value else None
            return _Cur()

    class _DB:
        def get_connection(self):
            return _Conn()

    return SimpleNamespace(db=_DB())


def test_pipeline_zero_close_change_pct_is_none():
    """②件-1: close=0 + prev_close>0 → change_pct is None (不是 -100, 不是 0)."""
    from data_layer.pipelines.kline import KlinePipeline
    pipe = KlinePipeline.__new__(KlinePipeline)
    kl = [_kline("2026-10-20", 0.0)]
    out = pipe._recompute_change_pct(_repo_with_prev(10.0), "600001", kl)
    assert len(out) == 1
    assert out[0].change_pct is None, (
        f"停牌日 change_pct 必须为 None (不可算), 实得 {out[0].change_pct!r}")
    assert out[0].change_pct != -100.0, "绝不能是 -100 (会把停牌读成跌停)"
    assert out[0].change_pct != 0.0, "绝不能是 0 (会被下游读成平盘)"


def test_pipeline_zero_close_does_not_poison_prev_close():
    """②件-2: close=0 后 prev_close 不被更新为 0 → 下一行仍能正常算 change_pct."""
    from data_layer.pipelines.kline import KlinePipeline
    pipe = KlinePipeline.__new__(KlinePipeline)
    kl = [
        _kline("2026-10-20", 0.0),      # 停牌日
        _kline("2026-10-21", 11.0),     # 次日正常
    ]
    out = pipe._recompute_change_pct(_repo_with_prev(10.0), "600001", kl)
    assert len(out) == 2
    assert out[0].change_pct is None, f"停牌日应 None; 实得 {out[0].change_pct!r}"
    assert out[1].change_pct == 10.0, (
        f"次日应按 prev_close=10.0 算出 +10.0%; 实得 {out[1].change_pct!r}")


def test_pipeline_normal_row_unchanged():
    """②件-3: close>0 的正常行 → change_pct 照常计算, 逐值不变."""
    from data_layer.pipelines.kline import KlinePipeline
    pipe = KlinePipeline.__new__(KlinePipeline)
    kl = [_kline("2026-10-20", 11.0)]
    out = pipe._recompute_change_pct(_repo_with_prev(10.0), "600001", kl)
    assert len(out) == 1
    assert out[0].change_pct == 10.0, f"应算 +10.0%; 实得 {out[0].change_pct!r}"
    assert out[0].pre_close == 10.0, f"pre_close 应保留; 实得 {out[0].pre_close}"


def test_pipeline_no_prev_close_keeps_none_for_zero():
    """②件-1 边界: 无 prev_close 且 close=0 → 仍 None (不可算), 不是 0."""
    from data_layer.pipelines.kline import KlinePipeline
    pipe = KlinePipeline.__new__(KlinePipeline)
    kl = [_kline("2026-10-20", 0.0)]
    out = pipe._recompute_change_pct(_repo_with_prev(0.0), "600001", kl)
    assert out[0].change_pct is None, (
        f"无 prev_close 时停牌日仍应 None; 实得 {out[0].change_pct!r}")
