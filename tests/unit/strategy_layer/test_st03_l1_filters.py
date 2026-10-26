"""ST-03 选股 L1 硬过滤补齐: 股价/日均成交额/停牌/次新股 (上市日期未知→拒绝)

阈值走 config (strategy.selection.l1.*); 停牌为近似判据 (待 DL-17 精确化);
次新股 list_date 未知拒绝 (fail-closed, 待 DL-21 回填后生效)。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.selection.selector import SelectionPipeline, _normalize_date

DATE = "2026-07-20"
PREV = "2026-07-17"


def fd(code, score=1.0):
    return FactorDaily(code=code, date=DATE, composite_score=score)


def kline(code, date, close=10.0, amount=1e8):
    return SimpleNamespace(code=code, date=date, close=close, amount=amount)


def make_dl(day_rows: dict[str, list], list_dates: dict[str, str]):
    """mock data_layer: snapshot_range 返回两日快照, get_stock_info 返回 list_date"""
    dl = MagicMock()
    # day_rows[code] = [kline(PREV), kline(DATE)] (停牌股只有 PREV 行)
    prev_ks = [rows[0] for rows in day_rows.values() if rows and rows[0].date == PREV]
    date_ks = [rows[-1] for rows in day_rows.values() if rows and rows[-1].date == DATE]
    dl.snapshot_range.return_value = [
        SimpleNamespace(klines=prev_ks),
        SimpleNamespace(klines=date_ks),
    ]

    def _info(code):
        return SimpleNamespace(list_date=list_dates.get(code, ""))
    dl.get_stock_info.side_effect = _info
    return dl


def rows(code, close=10.0, amount=1e8, both_days=True):
    out = [kline(code, PREV, close, amount)]
    if both_days:
        out.append(kline(code, DATE, close, amount))
    return out


class TestL1HardFilters:
    def _run(self, candidates, dl, cfg=None):
        pipe = SelectionPipeline(config=cfg or {}, data_layer=dl)
        return pipe.run(candidates, "neutral")

    def test_low_price_filtered(self):
        dl = make_dl({"A": rows("A", close=1.5)}, {"A": "2020-01-01"})
        r = self._run([fd("A")], dl)
        assert r.candidates == []
        assert any("L1 股价" in f and "1 只" in f for f in r.filters_applied)

    def test_low_amount_filtered(self):
        dl = make_dl({"A": rows("A", amount=1e6)}, {"A": "2020-01-01"})  # 100万 < 5000万
        r = self._run([fd("A")], dl)
        assert r.candidates == []
        assert any("L1 日均成交额" in f and "1 只" in f for f in r.filters_applied)

    def test_suspended_filtered_no_kline_on_date(self):
        """当日全市场有 kline 而该股无 → 停牌过滤 (近似判据)"""
        dl = make_dl({"A": rows("A"), "B": rows("B", both_days=False)},
                     {"A": "2020-01-01", "B": "2020-01-01"})
        r = self._run([fd("A"), fd("B")], dl)
        assert [c.code for c in r.candidates] == ["A"]
        assert any("L1 停牌" in f and "1 只" in f for f in r.filters_applied)

    def test_new_stock_filtered(self):
        """上市不足 60 交易日 → 过滤"""
        dl = make_dl({"A": rows("A")}, {"A": "2026-07-01"})  # 上市仅 ~13 交易日
        r = self._run([fd("A")], dl)
        assert r.candidates == []
        assert any("L1 次新" in f and "1 只" in f for f in r.filters_applied)

    def test_unknown_list_date_rejected_fail_closed(self):
        """ST-03 预决策: 上市日期未知 → 拒绝 (fail-closed, 待 DL-21 回填)"""
        dl = make_dl({"A": rows("A")}, {"A": ""})
        r = self._run([fd("A")], dl)
        assert r.candidates == []
        assert any("上市日期未知" in f and "1 只" in f for f in r.filters_applied)

    def test_healthy_stock_passes(self):
        dl = make_dl({"A": rows("A")}, {"A": "2020-01-01"})
        r = self._run([fd("A")], dl)
        assert [c.code for c in r.candidates] == ["A"]

    def test_thresholds_configurable(self):
        """阈值走 config: 放宽 min_price 后 1.5 元股通过"""
        dl = make_dl({"A": rows("A", close=1.5)}, {"A": "2020-01-01"})
        r = self._run([fd("A")], dl, cfg={"strategy.selection.l1.min_price": 1.0})
        assert [c.code for c in r.candidates] == ["A"]

    def test_no_data_layer_skips_hard_filters(self):
        """无 data_layer 注入时 L1b 跳过 (向后兼容, 仅 ST/退市过滤)"""
        pipe = SelectionPipeline(config={})
        r = pipe.run([fd("A")], "neutral")
        assert [c.code for c in r.candidates] == ["A"]
        assert not any("L1 股价" in f for f in r.filters_applied)

    def test_filters_applied_contains_all_l1_counts(self):
        """验收: filters_applied 含各 L1 项计数"""
        dl = make_dl({"A": rows("A")}, {"A": "2020-01-01"})
        r = self._run([fd("A")], dl)
        joined = " | ".join(r.filters_applied)
        for key in ("L1 股价", "L1 日均成交额", "L1 停牌", "L1 次新", "上市日期未知"):
            assert key in joined


class TestNormalizeDate:
    def test_yyyymmdd(self):
        assert _normalize_date("20260531") == "2026-05-31"

    def test_dashed(self):
        assert _normalize_date("2026-05-31") == "2026-05-31"

    def test_empty_or_garbage(self):
        assert _normalize_date("") == ""
        assert _normalize_date("N/A") == ""
