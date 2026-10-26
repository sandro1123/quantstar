"""因子聚合 — 加权求和 + 排名 + 五分位"""
import math
from dataclasses import dataclass, field

from common.logging import get_logger
from strategy_layer.engine import CleanedFactorMatrix
from strategy_layer.factors import compute_only_factor_names

logger = get_logger("strategy.aggregation")

DEFAULT_WEIGHTS = {
    "seasonality_same_month": 0.076,
    "std20": 0.069,
    "bb_width": 0.061,
    "klen": 0.059,
    "std10": 0.055,
    "cntp5": 0.054,
    "std5": 0.053,
    "volatility_reversal": 0.051,
    "cntp10": 0.050,
    "vma20": 0.047,
    "sump20": 0.046,
    "vma10": 0.046,
    "cntp20": 0.044,
    "cntd5": 0.044,
    "rank10": 0.043,
    "kup": 0.042,
    "vma5": 0.042,
    "cntd10": 0.042,
    "vstd10": 0.039,
    "sump5": 0.038,
}

IC_NEGATIVE_FACTORS = {"std20", "cntp5", "vma20", "bb_width", "sump20", "corr5",
                        "cntp10", "std10", "klen", "std5", "cntd5", "cntd10",
                        "sump5", "sump10", "vstd10", "vstd5", "sumd20", "max5",
                        "klow", "rank10", "kup", "vma10", "cntp20", "cntd20",
                        "kmid2", "sumd5", "sumd10", "macd_hist", "kmid", "kup2",
                        "roc5", "roc20", "ma10_dev", "momentum_5d_reversal",
                        "roc10", "ma5_dev", "roc60", "ksft2", "klow2", "cord20",
                        "ksft", "cord5", "rsi14", "rank5", "min5",
                        "industry_momentum", "seasonality_same_month", "vma5"}

# ST-05 方案A (决策账本 §十三, 2026-08-20): 注册表级 compute_only 标记。
# 不在 DEFAULT_WEIGHTS 内的活跃因子照常计算并全量入库 factor_daily (IC 观测数据源),
# 但不参与 composite_score 聚合; 推导与标记见 factors/__init__.py::compute_only_factor_names。
COMPUTE_ONLY_FACTORS = compute_only_factor_names()


@dataclass
class FactorDaily:
    code: str
    date: str
    composite_score: float
    factor_scores: dict[str, float] = field(default_factory=dict)
    rank: int = 0
    quantile: int = 0
    ml_score: float = 0.0
    sector: str = ""
    # 截至决策日 T 已实现的近期收益 (选股 L3.3 动量加成只允许用它)。
    recent_return: float | None = None
    # T→T+N 前向收益: 只允许进入滞后日的 IC 计算, 绝不允许进入选股排序
    # (2026-10-26 审计 S-1: 旧实现把前向收益写进 recent_return, 历史回放里等于未来信号)。
    forward_return: float | None = None


def _composite_with_contract(adjusted: dict[str, float], weights: dict[str, float]) -> tuple[float, list[str]]:
    """ST-05 显式聚合契约: composite_score 仅对权重表成员求和。

    按权重表成员资格显式过滤, 返回被跳过 (不在权重表内, 即 compute_only:
    照常计算入库供 IC 观测, 仅不参与聚合) 的因子名; 不再依赖
    weights.get(k, 0) 默认 0 的隐式行为。数值与改造前实现完全一致
    (对拍保证, 见 test_st05_factor_convergence)。

    Args:
        adjusted: {因子名: 已按 IC_NEGATIVE_FACTORS 调整方向的因子值}
        weights: 当前聚合权重表 (DEFAULT_WEIGHTS 或其派生表)
    Returns:
        (composite_score, 被跳过的因子名列表)
    """
    composite = 0.0
    skipped: list[str] = []
    for name, value in adjusted.items():
        if name not in weights:
            skipped.append(name)
            continue
        composite += weights[name] * value
    return composite, skipped


class Aggregator:
    def __init__(self):
        self._redundant_factors: set[str] = set()

    def detect_redundant_factors(self, matrix: CleanedFactorMatrix, threshold: float = 0.7):
        from strategy_layer.aggregation.factor_correlation import (
            compute_factor_correlations,
            detect_redundant_factors,
        )
        scores_by_factor: dict[str, dict[str, float]] = {}
        for code, scores in matrix.items():
            for fname, fval in scores.items():
                scores_by_factor.setdefault(fname, {})[code] = fval
        if len(scores_by_factor) >= 2:
            correlations = compute_factor_correlations(scores_by_factor)
            self._redundant_factors = detect_redundant_factors(correlations, threshold)

    def aggregate(self, matrix: CleanedFactorMatrix, weights: dict[str, float] | None = None,
                  use_correlation_filter: bool = False) -> list[FactorDaily]:
        weights = weights or DEFAULT_WEIGHTS

        if use_correlation_filter and len(self._redundant_factors) > 0:
            from strategy_layer.aggregation.factor_correlation import get_effective_weights
            weights = get_effective_weights(weights, self._redundant_factors)
            logger.info(f"相关性过滤: 已调整 {len(self._redundant_factors)} 个冗余因子权重")

        results = []
        skipped_counter: dict[str, int] = {}

        for code, scores in matrix.items():
            adjusted = {}
            for k, v in scores.items():
                if k in IC_NEGATIVE_FACTORS:
                    adjusted[k] = -v
                else:
                    adjusted[k] = v
            composite, skipped = _composite_with_contract(adjusted, weights)
            for name in skipped:
                skipped_counter[name] = skipped_counter.get(name, 0) + 1
            results.append(FactorDaily(
                code=code, date=matrix.date,
                composite_score=composite, factor_scores=scores,
            ))

        if skipped_counter:
            logger.info(
                f"ST-05 聚合契约: {len(weights)} 个权重表因子参与 composite 聚合; "
                f"{len(skipped_counter)} 个无权重因子 (compute_only) 被显式跳过 "
                f"(不进 composite, 照常计算入库供 IC 观测), 如 {sorted(skipped_counter)[:5]}"
            )

        # 按 composite_score 降序排名
        results.sort(key=lambda x: x.composite_score, reverse=True)
        n = len(results) if results else 1
        for i, r in enumerate(results):
            r.rank = i + 1
            r.quantile = min(5, max(1, int(i / n * 5) + 1))

        logger.info(f"Aggregated {len(results)} stocks, top: {results[0].code if results else 'N/A'}")
        return results

    @staticmethod
    def fill_recent_returns(factor_dailies: list[FactorDaily], data_layer, date: str, horizon: int = 5) -> int:
        """填充 FactorDaily.forward_return (F3.2 断链1, 仅供 IC 观测)。

        从 data_layer.forward_return 读 T+horizon 前向收益写入 fd.forward_return。
        注意: 不再写 recent_return — 前向收益进入选股排序就是前视泄漏 (审计 S-1)。
        当日的前向收益通常尚未实现 (返回 None), 属正常; 返回成功填充的条数。
        """
        if data_layer is None or not hasattr(data_layer, "forward_return"):
            return 0
        filled = 0
        for fd in factor_dailies:
            try:
                ret = data_layer.forward_return(fd.code, date, horizon)
            except Exception:
                ret = None
            # 仅接受有限数值 (防御 mock/脏数据返回非 float, 下游会与 float 比较)
            if isinstance(ret, (int, float)) and not isinstance(ret, bool) and math.isfinite(ret):
                fd.forward_return = float(ret)
                filled += 1
        if filled:
            logger.info(f"fill_recent_returns: {filled}/{len(factor_dailies)} stocks (horizon={horizon}d)")
        return filled

    @staticmethod
    def aggregate_single(scores: dict[str, float], weights: dict[str, float] | None = None, ml_score: float = 0.0) -> float:
        weights = weights or DEFAULT_WEIGHTS
        adjusted = {}
        for k, v in scores.items():
            adjusted[k] = -v if k in IC_NEGATIVE_FACTORS else v
        # ST-05 显式契约: 仅权重表成员参与聚合 (与 aggregate 同一实现, 对拍保证一致)
        composite, _skipped = _composite_with_contract(adjusted, weights)
        if ml_score != 0.0:
            composite = composite * 0.8 + ml_score * 0.2
        return composite

    @staticmethod
    def get_quantile_distribution(factor_dailies: list[FactorDaily]) -> dict:
        if not factor_dailies:
            return {"quantile_counts": {}, "mean_scores": {}, "top_bottom_spread": 0.0}
        counts = {}
        scores = {}
        for fd in factor_dailies:
            q = fd.quantile
            counts[q] = counts.get(q, 0) + 1
            if q not in scores:
                scores[q] = []
            scores[q].append(fd.composite_score)
        mean_scores = {q: sum(v) / len(v) for q, v in scores.items()}
        q1_mean = mean_scores.get(1, 0.0)
        q5_mean = mean_scores.get(5, 0.0)
        return {
            "quantile_counts": {k: counts.get(k, 0) for k in range(1, 6)},
            "mean_scores": {k: round(mean_scores.get(k, 0.0), 4) for k in range(1, 6)},
            "top_bottom_spread": round(q1_mean - q5_mean, 4),
        }
