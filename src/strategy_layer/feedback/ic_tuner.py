"""ICTuner — 基于 IC 的因子权重调优"""
import math
from dataclasses import dataclass

from scipy.stats import spearmanr

from common.logging import get_logger

logger = get_logger("strategy.feedback")


@dataclass
class FactorIC:
    factor_name: str
    ic_value: float
    ir_value: float
    trend: str  # "up" | "down" | "stable"
    weight: float
    t_statistic: float = 0.0
    p_value: float = 1.0


@dataclass
class ICSignificance:
    ic: float
    t_statistic: float
    p_value: float
    significant: bool


class ICTuner:
    def __init__(self, lookback_days: int = 20):
        self.lookback_days = lookback_days
        self._ic_history: dict[str, list[float]] = {}
        self._sector_map: dict[str, str] = {}

    def set_sector_map(self, sector_map: dict[str, str]):
        self._sector_map = sector_map

    def compute_ic(self, factor_scores: dict[str, float], forward_returns: dict[str, float]) -> dict[str, float]:
        ic_results = {}
        for factor_name in factor_scores:
            codes = list(factor_scores[factor_name].keys())
            scores = [factor_scores[factor_name][c] for c in codes]
            returns = [forward_returns.get(c, 0) for c in codes]
            if len(scores) < 10 or len(set(scores)) < 2:
                ic_results[factor_name] = 0.0
                continue
            try:
                ic, _ = spearmanr(scores, returns)
                ic_results[factor_name] = ic if not math.isnan(ic) else 0.0
            except Exception:
                ic_results[factor_name] = 0.0
        return ic_results

    def compute_ic_with_significance(self, factor_scores: dict[str, float],
                                      forward_returns: dict[str, float]) -> dict[str, ICSignificance]:
        results = {}
        for factor_name in factor_scores:
            codes = list(factor_scores[factor_name].keys())
            scores = [factor_scores[factor_name][c] for c in codes]
            returns = [forward_returns.get(c, 0) for c in codes]
            if len(scores) < 10 or len(set(scores)) < 2:
                results[factor_name] = ICSignificance(ic=0.0, t_statistic=0.0, p_value=1.0, significant=False)
                continue
            try:
                ic, p = spearmanr(scores, returns)
                n = len(scores)
                t_stat = ic * math.sqrt((n - 2) / (1 - ic * ic)) if abs(ic) < 1 else 0.0
                results[factor_name] = ICSignificance(
                    ic=ic if not math.isnan(ic) else 0.0,
                    t_statistic=t_stat,
                    p_value=p if not math.isnan(p) else 1.0,
                    significant=p < 0.05,
                )
            except Exception:
                results[factor_name] = ICSignificance(ic=0.0, t_statistic=0.0, p_value=1.0, significant=False)
        return results

    def compute_ic_sector_neutral(self, factor_scores: dict[str, float],
                                   forward_returns: dict[str, float]) -> dict[str, float]:
        ic_results = {}
        for factor_name in factor_scores:
            codes = list(factor_scores[factor_name].keys())
            if not self._sector_map:
                ic_results[factor_name] = self.compute_ic(factor_scores, forward_returns).get(factor_name, 0.0)
                continue
            sectors = set(self._sector_map.get(c, "unknown") for c in codes)
            demeaned_scores = {}
            demeaned_returns = {}
            for sector in sectors:
                sector_codes = [c for c in codes if self._sector_map.get(c, "unknown") == sector]
                if len(sector_codes) < 3:
                    continue
                s_scores = [factor_scores[factor_name][c] for c in sector_codes]
                s_returns = [forward_returns.get(c, 0) for c in sector_codes]
                s_mean = sum(s_scores) / len(s_scores)
                r_mean = sum(s_returns) / len(s_returns)
                for c in sector_codes:
                    demeaned_scores[c] = factor_scores[factor_name][c] - s_mean
                    demeaned_returns[c] = forward_returns.get(c, 0) - r_mean
            all_codes = list(demeaned_scores.keys())
            if len(all_codes) < 10:
                ic_results[factor_name] = 0.0
                continue
            d_scores = [demeaned_scores[c] for c in all_codes]
            d_returns = [demeaned_returns[c] for c in all_codes]
            try:
                ic, _ = spearmanr(d_scores, d_returns)
                ic_results[factor_name] = ic if not math.isnan(ic) else 0.0
            except Exception:
                ic_results[factor_name] = 0.0
        return ic_results

    def update_weights(self, daily_ic: dict[str, float], current_weights: dict[str, float]) -> dict[str, float]:
        alpha = 0.1
        new_weights = {}
        for factor_name, ic in daily_ic.items():
            old_w = current_weights.get(factor_name, 0.0)
            adjustment = alpha * ic
            new_weights[factor_name] = old_w + adjustment
            self._ic_history.setdefault(factor_name, []).append(ic)
        total = sum(new_weights.values()) or 1.0
        normalized = {k: v / total for k, v in new_weights.items()}
        logger.info(f"ICTuner: adjusted weights for {len(normalized)} factors")
        return normalized

    def compute_ic_sector_neutral_enhanced(self, factor_values: list[float],
                                            forward_returns: list[float], sectors: list[str]) -> dict:
        import numpy as np
        from scipy.stats import spearmanr

        unique_sectors = list(set(sectors))
        adjusted_factors = np.array(factor_values, dtype=np.float64)
        adjusted_returns = np.array(forward_returns, dtype=np.float64)

        for sector in unique_sectors:
            mask = np.array([s == sector for s in sectors])
            if mask.sum() < 5:
                continue
            adjusted_factors[mask] = adjusted_factors[mask] - np.mean(adjusted_factors[mask])
            adjusted_returns[mask] = adjusted_returns[mask] - np.mean(adjusted_returns[mask])

        valid = ~(np.isnan(adjusted_factors) | np.isnan(adjusted_returns))
        if valid.sum() < 10:
            return {"sector_neutral_ic": 0.0, "sample_size": int(valid.sum()), "is_significant": False}

        corr, p_value = spearmanr(adjusted_factors[valid], adjusted_returns[valid])

        return {
            "sector_neutral_ic": round(float(corr), 4),
            "p_value": round(float(p_value), 4),
            "is_significant": p_value < 0.05,
            "sample_size": int(valid.sum()),
            "sectors_used": len(unique_sectors),
        }

    def update_weights_with_decay(self, current_weights: dict[str, float],
                                   ic_history: dict[str, list[float]], half_life: int = 20,
                                   weight_history: list[dict[str, float]] | None = None,
                                   is_monday: bool = False) -> dict[str, float]:
        """更新因子权重，支持稳定性保护:
        1. 20% 日变化上限
        2. 连续 3 天同向才生效
        3. 最低权重 0.01
        4. 周一平滑 (70% 上周 + 30% 本周)
        """
        import numpy as np

        new_weights = {}
        for factor, ics in ic_history.items():
            if len(ics) < 5:
                new_weights[factor] = current_weights.get(factor, 0.0)
                continue

            arr = np.array(ics)
            n = len(arr)
            decay = np.array([0.5 ** (i / half_life) for i in range(n)])
            decay = decay / decay.sum()

            weighted_ic = float(np.sum(arr * decay))
            new_weights[factor] = max(0, weighted_ic)

        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        # 保护 1: 单日变化不超过 20%
        for factor in new_weights:
            old_w = current_weights.get(factor, 0)
            new_w = new_weights[factor]
            max_change = max(0.01, old_w * 0.2)
            if new_w > old_w + max_change:
                new_weights[factor] = old_w + max_change
            elif new_w < old_w - max_change:
                new_weights[factor] = old_w - max_change

        # 保护 2: 连续 3 天同向才生效
        if weight_history and len(weight_history) >= 3:
            recent_3 = weight_history[-3:]
            for factor in new_weights:
                old_w = current_weights.get(factor, 0)
                new_w = new_weights[factor]
                prev_w = old_w
                changes = []
                for hist in recent_3:
                    changes.append(hist.get(factor, prev_w) - prev_w)
                    prev_w = hist.get(factor, prev_w)
                all_positive = all(c > 0.001 for c in changes)
                all_negative = all(c < -0.001 for c in changes)
                if not (all_positive or all_negative):
                    new_weights[factor] = old_w

        # 保护 3: 最低权重 0.01
        for factor in new_weights:
            new_weights[factor] = max(new_weights[factor], 0.01)

        # 保护 4: 周一平滑 (70% 上周 + 30% 本周)
        if is_monday and weight_history:
            last_week = weight_history[-1] if weight_history else current_weights
            for factor in new_weights:
                old_w = last_week.get(factor, current_weights.get(factor, 0))
                new_weights[factor] = old_w * 0.7 + new_weights[factor] * 0.3

        # 最终归一化
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        return new_weights

    def get_factor_ic(self, factor_name: str | None = None) -> "FactorIC | dict[str, float] | None":
        """取因子 IC。

        双形态 (F席 09-16 契约修):
          - get_factor_ic(name)  → FactorIC | None   (既有契约, 向后兼容)
          - get_factor_ic()      → {factor: mean_ic} (全因子概览, 报告段消费形态)
        旧实现无参必 TypeError, 被调用方 except 吞成静默降级 (ic_weight.py)。
        """
        if factor_name is None:
            out: dict[str, float] = {}
            for _f, _h in self._ic_history.items():
                if _h:
                    out[_f] = sum(_h[-self.lookback_days:]) / len(_h[-self.lookback_days:])
            return out
        history = self._ic_history.get(factor_name, [])
        if not history:
            return None
        recent = history[-self.lookback_days:]
        mean_ic = sum(recent) / len(recent)
        if len(recent) >= 2:
            std = math.sqrt(sum((x - mean_ic) ** 2 for x in recent) / len(recent)) or 0.001
            ir = mean_ic / std
        else:
            ir = 0.0
        if len(history) >= 3:
            recent_trend = history[-3:]
            trend = "up" if recent_trend[-1] > recent_trend[0] else ("down" if recent_trend[-1] < recent_trend[0] else "stable")
        else:
            trend = "stable"
        n = len(recent)
        t_stat = mean_ic * math.sqrt(n) / std if len(recent) >= 2 and std > 0 else 0.0
        from scipy.stats import t as t_dist
        p_val = 2 * (1 - t_dist.cdf(abs(t_stat), n - 1)) if len(recent) >= 2 and t_dist.cdf(abs(t_stat), n - 1) < 1 else 1.0
        return FactorIC(factor_name=factor_name, ic_value=mean_ic, ir_value=ir, trend=trend, weight=0.0, t_statistic=t_stat, p_value=p_val)
