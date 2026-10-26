"""IC Calculator — 因子IC计算"""

from dataclasses import dataclass

import numpy as np
from scipy.stats import spearmanr


@dataclass
class ICResult:
    mean_ic: float
    std_ic: float
    icir: float
    t_stat: float
    p_value: float
    is_significant: bool
    sample_size: int
    detail: dict | None = None


class ICCalculator:
    def compute_ic(self, factor_scores: dict, forward_returns: dict) -> float:
        """Spearman rank correlation IC"""
        codes = [c for c in factor_scores if c in forward_returns]
        if len(codes) < 10:
            return 0.0
        f_scores = [factor_scores[c] for c in codes]
        f_returns = [forward_returns[c] for c in codes]
        n = len(codes)
        rank_x = sorted(f_scores)
        rank_y = sorted(f_returns)
        rx = {v: i for i, v in enumerate(rank_x)}
        ry = {v: i for i, v in enumerate(rank_y)}
        d_sq = sum((rx[s] - ry[r]) ** 2 for s, r in zip(f_scores, f_returns))
        return 1 - 6 * d_sq / (n * (n * n - 1)) if n > 1 else 0.0

    def compute_ic_with_significance(
        self, factor_values: list[float], forward_returns: list[float]
    ) -> ICResult:
        """Compute IC with t-stat, p-value, and significance flag"""
        x = np.array(factor_values, dtype=float)
        y = np.array(forward_returns, dtype=float)

        mask = ~(np.isnan(x) | np.isnan(y))
        x, y = x[mask], y[mask]
        n = len(x)

        if n < 10:
            return ICResult(
                mean_ic=0.0,
                std_ic=0.0,
                icir=0.0,
                t_stat=0.0,
                p_value=1.0,
                is_significant=False,
                sample_size=n,
            )

        corr, p_value = spearmanr(x, y)
        t_stat = corr * np.sqrt((n - 2) / (1 - corr**2)) if abs(corr) < 1 else 0.0
        std_y = float(np.std(y))
        icir = float(corr / std_y) if std_y > 0 else 0.0

        return ICResult(
            mean_ic=float(corr),
            std_ic=std_y,
            icir=icir,
            t_stat=float(t_stat),
            p_value=float(p_value),
            is_significant=p_value < 0.05,
            sample_size=n,
        )

    def factor_rank_autocorrelation(
        self, factor_values_by_date: dict[str, list[float]]
    ) -> dict:
        """Compute factor rank autocorrelation (turnover/stability)"""
        dates = sorted(factor_values_by_date.keys())
        corrs = []
        for i in range(len(dates) - 1):
            today = np.array(factor_values_by_date[dates[i]], dtype=float)
            tomorrow = np.array(factor_values_by_date[dates[i + 1]], dtype=float)
            mask = ~(np.isnan(today) | np.isnan(tomorrow))
            if mask.sum() > 10:
                corr, _ = spearmanr(today[mask], tomorrow[mask])
                corrs.append(corr)

        return {
            "mean_autocorrelation": float(np.mean(corrs)) if corrs else 0.0,
            "std_autocorrelation": float(np.std(corrs)) if corrs else 0.0,
            "n_periods": len(corrs),
        }

    def compute_strategy_ic(
        self,
        composite_scores: list[float],
        forward_returns: list[float],
        factor_ics: list[dict],
    ) -> dict:
        """Compute strategy-level IC from composite scores"""
        x = np.array(composite_scores)
        y = np.array(forward_returns)
        mask = ~(np.isnan(x) | np.isnan(y))
        x, y = x[mask], y[mask]

        if len(x) < 10:
            return {"strategy_ic": 0.0, "sample_size": len(x), "is_significant": False}

        corr, p_value = spearmanr(x, y)
        n = len(x)
        t_stat = corr * np.sqrt((n - 2) / (1 - corr**2)) if abs(corr) < 1 else 0

        contributions = {}
        for fic in factor_ics:
            fname = fic.get("factor_name", "unknown")
            fic_val = fic.get("mean_ic", 0)
            contributions[fname] = fic_val

        sorted_factors = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)
        top_factors = [{"factor": f, "ic": round(v, 4)} for f, v in sorted_factors[:5]]

        return {
            "strategy_ic": round(float(corr), 4),
            "t_stat": round(float(t_stat), 4),
            "p_value": round(float(p_value), 4),
            "is_significant": p_value < 0.05,
            "sample_size": int(n),
            "factor_count": len(contributions),
            "top_factors": top_factors,
        }
