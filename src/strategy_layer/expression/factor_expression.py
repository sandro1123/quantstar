"""因子表达式引擎 — 注册字符串表达式 → 自动生成因子

支持 WorldQuant 101 Alpha 格式:
  - 算子: rank(), ts_sum(), ts_rank(), ts_corr(), ts_covariance(), group_rank()...
  - 运算: +, -, *, /, >, <, >=, <=, ==, !=
  - 数据引用: close, open, high, low, volume, vwap, returns...

用法:
  expr = FactorExpression()
  expr.register("alpha_001", "rank(ts_argmax(signedpower((returns < 0 ? stddev(returns, 20) : close), 2.), 5))")
  factor = expr.build("alpha_001")  # 返回 BaseFactor 子类
"""
from strategy_layer.expression.parser import evaluate_series, parse, tokenize
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class FactorExpression:
    def __init__(self):
        self._registry: dict[str, str] = {}

    def register(self, name: str, expression: str, description: str = ""):
        self._registry[name] = expression

    def unregister(self, name: str):
        self._registry.pop(name, None)

    def list_expressions(self) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self._registry.items()]

    def get_expression(self, name: str) -> str | None:
        return self._registry.get(name)

    def build(self, name: str) -> type[BaseFactor]:
        expr = self._registry.get(name)
        if not expr:
            raise ValueError(f"Expression '{name}' not registered")

        tokens = tokenize(expr)
        ast = parse(tokens)

        class DynamicFactor(BaseFactor):
            description = f"Alpha expression: {expr[:60]}..."
            category = "alpha"
            window_size = 250  # alpha_019 需要 ts_sum(returns, 250)

            def compute(self, ctx: FactorContext) -> FactorResult:
                try:
                    context = self._build_context(ctx)
                    series = evaluate_series(ast, context)
                    score = series[-1] if series else 0.0
                    return FactorResult(
                        code=ctx.code, date=ctx.date,
                        factor_name=self.name,
                        score=round(float(score), 6),
                        raw_value=float(score),
                    )
                except Exception:
                    # 计算失败返回 None (被清洗层过滤), 不写静默 0
                    return FactorResult(
                        code=ctx.code, date=ctx.date,
                        factor_name=self.name,
                        score=None, raw_value=None,
                    )

            @staticmethod
            def _build_context(ctx: FactorContext) -> dict:
                closes = [k.close if hasattr(k, "close") else k.get("close", 0) for k in ctx.klines]
                volumes = [k.volume if hasattr(k, "volume") else k.get("volume", 0) for k in ctx.klines]
                highs = [k.high if hasattr(k, "high") else k.get("high", 0) for k in ctx.klines]
                lows = [k.low if hasattr(k, "low") else k.get("low", 0) for k in ctx.klines]
                opens = [k.open if hasattr(k, "open") else k.get("open", 0) for k in ctx.klines]
                returns = [0.0]
                for i in range(1, len(closes)):
                    p = closes[i-1] if closes[i-1] != 0 else 1e-8
                    returns.append((closes[i] - closes[i-1]) / p)
                vwap = [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes)] if len(highs) == len(closes) else closes
                return {
                    "close": closes[-1] if closes else 0,
                    "open": opens[-1] if opens else 0,
                    "high": highs[-1] if highs else 0,
                    "low": lows[-1] if lows else 0,
                    "volume": volumes[-1] if volumes else 0,
                    "vwap": vwap[-1] if vwap else 0,
                    "returns": returns[-1] if returns else 0,
                    "close_ts": closes,
                    "volume_ts": volumes,
                    "high_ts": highs,
                    "low_ts": lows,
                    "open_ts": opens,
                    "returns_ts": returns,
                    "vwap_ts": vwap,
                }

        DynamicFactor.__name__ = f"AlphaExpression_{name}"
        DynamicFactor.name = name
        return DynamicFactor

    def build_and_register(self, name: str, expression: str) -> type[BaseFactor]:
        self.register(name, expression)
        return self.build(name)


# 内置 Alpha 表达式注册表
ALPHA_EXPRESSIONS = {
    "alpha_001": "(rank(ts_argmax(signedpower(((returns < 0) ? ts_stddev(returns, 20) : close), 2.), 5)) - 0.5)",
    "alpha_002": "(-1 * correlation(rank(delta(log(volume), 2)), rank(((close - open) / open)), 6))",
    "alpha_003": "(-1 * correlation(rank(open), rank(volume), 10))",
    "alpha_004": "(-1 * ts_rank(rank(low), 9))",
    "alpha_005": "(rank((open - (ts_sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap)))))",
    "alpha_006": "(-1 * correlation(open, volume, 10))",
    "alpha_007": "((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1))",
    "alpha_008": "(-1 * rank(((ts_sum(open, 5) * ts_sum(returns, 5)) - delay((ts_sum(open, 5) * ts_sum(returns, 5)), 10))))",
    "alpha_009": "((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(delta(close, 1), 5) < 0) ? delta(close, 1) : (-1 * delta(close, 1))))",
    "alpha_010": "rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : ((ts_max(delta(close, 1), 4) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))))",
    "alpha_011": "((rank(ts_max((vwap - close), 3)) + rank(ts_min((vwap - close), 3))) * rank(delta(volume, 3)))",
    "alpha_012": "(signedpower(delta(volume, 1), 3) / volume)",
    "alpha_013": "rank(ts_sum((high - low), 3) / ts_sum(volume, 3))",
    "alpha_014": "((-1 * rank(delta(returns, 3))) * correlation(open, volume, 10))",
    "alpha_015": "(-1 * ts_rank(ts_corr(rank(high), rank(volume), 3), 3))",
    "alpha_016": "(-1 * rank(ts_covariance(rank(high), rank(volume), 5)))",
    "alpha_017": "((-1 * rank(ts_rank(close, 10))) * rank(delta(delta(close, 1), 1)))",
    "alpha_018": "(-1 * rank(((ts_stddev(abs(close - open), 5) + (close - open)) + correlation(close, open, 10))))",
    "alpha_019": "((-1 * sign(((close - delay(close, 7)) + delta(close, 7)))) * (1 + rank((1 + ts_sum(returns, 250)))))",
    "alpha_020": "(((-1 * rank((open - delay(high, 1)))) * rank((open - delay(close, 1)))) * rank((open - delay(low, 1))))",
}


def register_alpha_expression(name: str, expression: str) -> type[BaseFactor]:
    """注册单个 Alpha 表达式并返回因子类"""
    engine = FactorExpression()
    return engine.build_and_register(name, expression)


def register_all_alpha_expressions() -> list[type[BaseFactor]]:
    """注册所有内置 Alpha 表达式"""
    engine = FactorExpression()
    factors = []
    for name, expr in ALPHA_EXPRESSIONS.items():
        try:
            factors.append(engine.build_and_register(name, expr))
        except Exception as e:
            import logging
            logging.getLogger("strategy.expression").warning(f"Failed to register {name}: {e}")
    return factors
