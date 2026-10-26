"""Tests for SelectionPipeline L0-L3 filtering"""
from strategy_layer.selection.selector import SelectionPipeline
from strategy_layer.aggregation.aggregator import FactorDaily


def _make_fd(code: str, score: float = 0.5, sector: str = "") -> FactorDaily:
    return FactorDaily(code=code, date="2026-07-24", composite_score=score,
                       factor_scores={}, sector=sector)


class TestSelectionPipeline:
    def test_empty_input_returns_empty(self):
        pipe = SelectionPipeline()
        result = pipe.run([])
        assert len(result.candidates) == 0
        assert result.total_evaluated == 0

    def test_l1_filters_st_stocks(self):
        # L1 现按调用方传入的 excluded_codes 过滤 (data_layer.get_risky_codes 按名称匹配 ST/退)
        pipe = SelectionPipeline()
        candidates = [_make_fd("300716"), _make_fd("000001")]  # 300716 = *ST泉为
        result = pipe.run(candidates, excluded_codes={"300716"})
        assert all(c.code != "300716" for c in result.candidates)
        assert "000001" in [c.code for c in result.candidates]
        assert any("ST" in f for f in result.filters_applied)

    def test_l1_no_excluded_codes_keeps_all(self):
        # fail-open: 未传 excluded_codes (查询失败/空集) 时不过滤
        pipe = SelectionPipeline()
        candidates = [_make_fd("300716"), _make_fd("000001")]
        result = pipe.run(candidates)
        assert len(result.candidates) == 2

    def test_l2_selects_top_n(self):
        pipe = SelectionPipeline(config={"strategy.selection.top_n": {"bull": 3, "neutral": 3, "bear": 3}})
        candidates = [_make_fd(f"{i:06d}", score=1.0 - i * 0.1) for i in range(10)]
        result = pipe.run(candidates, market_regime="bull")
        assert len(result.candidates) == 3

    def test_l2_respects_regime_top_n(self):
        pipe = SelectionPipeline(config={"strategy.selection.top_n": {"bull": 10, "bear": 4}})
        candidates = [_make_fd(f"{i:06d}", score=1.0 - i * 0.1) for i in range(10)]
        result = pipe.run(candidates, market_regime="bear")
        assert len(result.candidates) == 2

    def test_l3_sector_dedup(self):
        pipe = SelectionPipeline(config={"strategy.selection.sector_limit": 1})
        candidates = [_make_fd("000001", sector="IT"), _make_fd("000002", sector="IT"),
                      _make_fd("000003", sector="FIN")]
        result = pipe.run(candidates)
        codes = [c.code for c in result.candidates]
        assert "000001" in codes
        assert "000003" in codes
        assert len([c for c in codes if c in ("000001", "000002")]) <= 1

    def test_l3_bear_contracts(self):
        pipe = SelectionPipeline(config={"strategy.selection.top_n": {"bear": 10}})
        candidates = [_make_fd(f"{i:06d}", score=1.0) for i in range(10)]
        result = pipe.run(candidates, market_regime="bear")
        assert len(result.candidates) < 10

    def test_returns_correct_metadata(self):
        pipe = SelectionPipeline()
        candidates = [_make_fd("000001"), _make_fd("ST0002")]
        result = pipe.run(candidates)
        assert result.total_evaluated == 2
        assert len(result.filters_applied) > 0