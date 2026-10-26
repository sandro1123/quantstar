"""backtest_layer/evaluation/factor_eval.py — 因子评估完整实现

提供四个评估维度：
- FactorIC: IC/Rank IC/IC_IR/t-stat
- FactorTurnover: 换手率 + 平均持仓期
- FactorDecay: lag-N IC 衰减曲线
- FactorCorrelation: 因子间相关性矩阵
"""

import math
from dataclasses import dataclass, field


@dataclass
class FactorICResult:
    factor: str
    ic: float = 0.0
    rank_ic: float = 0.0
    # BT-09: IC_IR/t_stat 需多期 IC 时间序列, 单截面不可算 → None (原 ic_ir 恒 0.0、
    # std_ic=abs(ic)*0.5 编造估计均为假值)
    ic_ir: float | None = None
    t_stat: float | None = None
    valid: bool = False
    n_periods: int = 0


@dataclass
class FactorTurnoverResult:
    factor: str
    avg_turnover: float = 0.0
    max_turnover: float = 0.0
    avg_holding_days: float = 0.0
    n_periods: int = 0


@dataclass
class FactorDecayResult:
    factor: str
    decay_curve: list[float] = field(default_factory=list)
    half_life: int = -1


@dataclass
class FactorCorrelationResult:
    factors: list[str] = field(default_factory=list)
    matrix: list[list[float]] = field(default_factory=list)
    high_corr_pairs: list[tuple[str, str, float]] = field(default_factory=list)


def _pearson_ic(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    mx = sum(x[:n]) / n
    my = sum(y[:n]) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = sum((x[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((y[i] - my) ** 2 for i in range(n)) ** 0.5
    return num / (dx * dy) if dx * dy > 0 else 0.0


def _spearman_rank_ic(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0

    def _rank(arr: list[float]) -> list[float]:
        indexed = sorted(enumerate(arr), key=lambda t: t[1])
        ranks = [0.0] * len(arr)
        i = 0
        while i < len(indexed):
            j = i
            while j < len(indexed) - 1 and indexed[j + 1][1] == indexed[j][1]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    rx = _rank(x[:n])
    ry = _rank(y[:n])
    return _pearson_ic(rx, ry)


class FactorEvaluator:
    def __init__(self, risk_free_rate: float = 0.03):
        self.risk_free_rate = risk_free_rate
        self.factor_metrics: dict[str, dict] = {}

    def evaluate(self, factor_scores: dict[str, list[float]], forward_returns: list[float]) -> dict[str, dict]:
        results = {}
        for factor_name, scores in factor_scores.items():
            ic_result = self.compute_ic(factor_name, scores, forward_returns)
            results[factor_name] = {
                "ic": ic_result.ic,
                "rank_ic": ic_result.rank_ic,
                "ic_ir": ic_result.ic_ir,
                "t_stat": ic_result.t_stat,
                "valid": ic_result.valid,
                "n_periods": ic_result.n_periods,
            }
        self.factor_metrics = results
        return results

    def compute_ic(self, factor_name: str, scores: list[float], forward_returns: list[float]) -> FactorICResult:
        n = min(len(scores), len(forward_returns))
        if n < 3:
            return FactorICResult(factor=factor_name, valid=False, n_periods=n)
        s = scores[:n]
        r = forward_returns[:n]
        ic = _pearson_ic(s, r)
        rank_ic = _spearman_rank_ic(s, r)
        # BT-09: 单截面只有 1 个 IC 观测值 — ic_ir (IC 均值/IC 标准差) 与 t_stat
        # (ic*sqrt(n_periods)/std_ic) 都需要多期 IC 时间序列才能算。
        # 原实现 std_ic = abs(ic)*0.5 是编造估计, ic_ir 恒 0.0 是假值 —
        # 均改为 None 显式"不可算", 多期评估走 compute_ic_series (宁可没有, 不可是假的)。
        return FactorICResult(
            factor=factor_name,
            ic=round(ic, 4),
            rank_ic=round(rank_ic, 4),
            ic_ir=None,
            t_stat=None,
            valid=True,
            n_periods=n,
        )

    def compute_ic_series(self, factor_name: str,
                          scores_by_period: list[list[float]],
                          returns_by_period: list[list[float]]) -> FactorICResult:
        """BT-09: 多期 IC 时间序列评估 — ic_ir 与 t_stat 真算。

        每期一个截面 (scores_by_period[i] × returns_by_period[i]) 算一个 IC,
        ic_ir = mean(ic_series) / std(ic_series), t_stat = ic_ir * sqrt(n_periods)。
        期数 < 2 或 std=0 时 ic_ir/t_stat 为 None (不可算)。
        """
        n_periods = min(len(scores_by_period), len(returns_by_period))
        ic_series = []
        for i in range(n_periods):
            n = min(len(scores_by_period[i]), len(returns_by_period[i]))
            if n < 3:
                continue
            ic_series.append(_pearson_ic(scores_by_period[i][:n], returns_by_period[i][:n]))
        if len(ic_series) < 2:
            return FactorICResult(factor=factor_name, valid=False, n_periods=len(ic_series))
        mean_ic = sum(ic_series) / len(ic_series)
        std_ic = (sum((x - mean_ic) ** 2 for x in ic_series) / len(ic_series)) ** 0.5
        ic_ir = mean_ic / std_ic if std_ic > 0 else None
        t_stat = ic_ir * math.sqrt(len(ic_series)) if ic_ir is not None else None
        return FactorICResult(
            factor=factor_name,
            ic=round(mean_ic, 4),
            rank_ic=round(mean_ic, 4),
            ic_ir=round(ic_ir, 4) if ic_ir is not None else None,
            t_stat=round(t_stat, 4) if t_stat is not None else None,
            valid=True,
            n_periods=len(ic_series),
        )

    def compute_turnover(self, factor_name: str, holdings_sequence: list[list[str]]) -> FactorTurnoverResult:
        if len(holdings_sequence) < 2:
            return FactorTurnoverResult(factor=factor_name, n_periods=len(holdings_sequence))
        turnovers = []
        for i in range(1, len(holdings_sequence)):
            prev = set(holdings_sequence[i - 1])
            curr = set(holdings_sequence[i])
            if not prev:
                turnovers.append(1.0)
                continue
            changed = len(prev.symmetric_difference(curr))
            turnovers.append(changed / (len(prev) + len(curr)) if (len(prev) + len(curr)) > 0 else 0.0)
        return FactorTurnoverResult(
            factor=factor_name,
            avg_turnover=round(sum(turnovers) / len(turnovers), 4) if turnovers else 0.0,
            max_turnover=round(max(turnovers), 4) if turnovers else 0.0,
            avg_holding_days=round(1.0 / (sum(turnovers) / len(turnovers)), 1) if turnovers and sum(turnovers) > 0 else 0.0,
            n_periods=len(holdings_sequence),
        )

    def compute_decay(self, factor_name: str, scores: list[float], forward_returns: list[float], max_lag: int = 10) -> FactorDecayResult:
        n = min(len(scores), len(forward_returns))
        if n < 3:
            return FactorDecayResult(factor=factor_name)
        decay = []
        for lag in range(min(max_lag + 1, n)):
            lagged_scores = scores[:n - lag] if lag > 0 else scores[:n]
            lagged_returns = forward_returns[lag:n] if lag > 0 else forward_returns[:n]
            common = min(len(lagged_scores), len(lagged_returns))
            if common < 3:
                decay.append(0.0)
            else:
                decay.append(round(_pearson_ic(lagged_scores[:common], lagged_returns[:common]), 4))
        half_life = -1
        if decay and decay[0] > 0:
            threshold = decay[0] / 2
            for i, v in enumerate(decay):
                if v < threshold:
                    half_life = i
                    break
        return FactorDecayResult(factor=factor_name, decay_curve=decay, half_life=half_life)


class FactorCorrelation:
    @staticmethod
    def compute(factor_scores: dict[str, list[float]], threshold: float = 0.7) -> FactorCorrelationResult:
        factors = list(factor_scores.keys())
        n = len(factors)
        if n == 0:
            return FactorCorrelationResult()
        min_len = min(len(factor_scores[f]) for f in factors)
        if min_len < 2:
            return FactorCorrelationResult(factors=factors)
        matrix = [[0.0] * n for _ in range(n)]
        high_corr = []
        for i in range(n):
            matrix[i][i] = 1.0
            for j in range(i + 1, n):
                ic = _pearson_ic(factor_scores[factors[i]][:min_len], factor_scores[factors[j]][:min_len])
                matrix[i][j] = round(ic, 4)
                matrix[j][i] = round(ic, 4)
                if abs(ic) >= threshold:
                    high_corr.append((factors[i], factors[j], round(ic, 4)))
        return FactorCorrelationResult(factors=factors, matrix=matrix, high_corr_pairs=high_corr)
