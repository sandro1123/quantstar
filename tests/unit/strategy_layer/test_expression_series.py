"""evaluate_series 序列求值测试 — alpha 表达式不再退化为 0"""
import math

from strategy_layer.expression.factor_expression import (
    ALPHA_EXPRESSIONS,
    FactorExpression,
    register_all_alpha_expressions,
)
from strategy_layer.expression.parser import evaluate_series, parse, tokenize
from strategy_layer.factors.base import FactorContext


def _make_ctx(n: int = 60) -> FactorContext:
    """带真实波动的 K 线序列"""
    from dataclasses import dataclass

    @dataclass
    class K:
        close: float
        open: float
        high: float
        low: float
        volume: float

    bars = []
    for i in range(n):
        c = 10.0 + 0.05 * i + 0.3 * math.sin(i / 3.0) + (0.15 if i % 7 == 0 else -0.08)
        bars.append(K(close=c, open=c - 0.04, high=c + 0.12, low=c - 0.12,
                      volume=1000 + 30 * i + 150 * math.sin(i / 2.0)))
    return FactorContext(code="600000", date="2026-08-14", klines=bars,
                         financials=[], valuations=[], moneyflow=[],
                         concept_data=[], lhb_data=[], analyst_data=[],
                         sentiment_data={}, sector="")


def _series_ctx(n: int = 30) -> dict:
    closes = [10.0 + i * 0.1 + (0.2 if i % 3 == 0 else -0.1) for i in range(n)]
    volumes = [1000.0 + 50 * i + (200 if i % 4 == 0 else 0) for i in range(n)]
    highs = [c + 0.15 for c in closes]
    lows = [c - 0.15 for c in closes]
    opens = [c - 0.05 for c in closes]
    returns = [0.0] + [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, n)]
    vwap = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    return {
        "close": closes[-1], "open": opens[-1], "high": highs[-1], "low": lows[-1],
        "volume": volumes[-1], "vwap": vwap[-1], "returns": returns[-1],
        "close_ts": closes, "volume_ts": volumes, "high_ts": highs, "low_ts": lows,
        "open_ts": opens, "returns_ts": returns, "vwap_ts": vwap,
    }


class TestEvaluateSeries:
    def test_returns_list_matching_series_length(self):
        ctx = _series_ctx(30)
        ast = parse(tokenize(ALPHA_EXPRESSIONS["alpha_001"]))
        series = evaluate_series(ast, ctx)
        assert isinstance(series, list)
        assert len(series) == 30

    def test_nested_ts_operators_propagate_series(self):
        # alpha_008: delay(ts_sum(open,5)*ts_sum(returns,5), 10) — 嵌套时序算子
        ctx = _series_ctx(30)
        ast = parse(tokenize(ALPHA_EXPRESSIONS["alpha_008"]))
        series = evaluate_series(ast, ctx)
        assert len(series) == 30
        assert any(abs(v) > 1e-12 for v in series)

    def test_most_alphas_nonzero_on_varying_data(self):
        ctx = _series_ctx(30)
        nonzero = 0
        for expr in ALPHA_EXPRESSIONS.values():
            series = evaluate_series(parse(tokenize(expr)), ctx)
            if abs(series[-1]) > 1e-12:
                nonzero += 1
        assert nonzero >= 15, f"only {nonzero}/20 alphas nonzero"

    def test_empty_context_does_not_crash(self):
        ast = parse(tokenize(ALPHA_EXPRESSIONS["alpha_001"]))
        series = evaluate_series(ast, {"close_ts": [], "volume_ts": []})
        assert isinstance(series, list) and len(series) >= 1

    def test_abs_builtin_fallback(self):
        # alpha_007/012/018 用到 abs() — 算子库无此名, 需 builtins 兜底
        ctx = _series_ctx(30)
        ast = parse(tokenize(ALPHA_EXPRESSIONS["alpha_018"]))
        series = evaluate_series(ast, ctx)
        assert len(series) == 30


class TestDynamicFactorCompute:
    def test_compute_returns_nonzero_scores(self):
        ctx = _make_ctx(60)
        factors = register_all_alpha_expressions()
        assert len(factors) == 20
        nonzero = 0
        for cls in factors:
            res = cls().compute(ctx)
            if res.score is not None and abs(res.score) > 1e-12:
                nonzero += 1
        assert nonzero >= 15, f"only {nonzero}/20 nonzero"

    def test_compute_failure_returns_none_not_zero(self):
        eng = FactorExpression()
        cls = eng.build_and_register("test_bad", "nonexistent_func(close)")
        res = cls().compute(_make_ctx(10))
        assert res.score is None

    def test_window_size_covers_alpha_019(self):
        factors = register_all_alpha_expressions()
        assert all(f.window_size >= 250 for f in factors)
