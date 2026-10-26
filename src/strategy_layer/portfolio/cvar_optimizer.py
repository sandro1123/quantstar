"""CVaR 尾部风险优化器 — 基于 Rockafellar & Uryasev (2000) 线性规划形式"""
from dataclasses import dataclass

import numpy as np


@dataclass
class CVaRResult:
    weights: dict[str, float]
    cvar: float
    var: float
    expected_return: float


class CVaROptimizer:
    def __init__(self, alpha: float = 0.95, risk_free_rate: float = 0.02):
        self.alpha = alpha
        self.risk_free_rate = risk_free_rate

    def optimize(
        self,
        expected_returns: dict[str, float],
        covariance: dict[str, dict[str, float]] = None,
        scenarios: np.ndarray = None,
        max_cvar: float = None,
        min_return: float = None,
        min_weight: float = 0.0,
        max_weight: float = 1.0,
    ) -> CVaRResult:
        codes = list(expected_returns.keys())
        n = len(codes)

        if n == 0:
            return CVaRResult(weights={}, cvar=0, var=0, expected_return=0)

        mu = np.array([expected_returns[c] for c in codes])

        if scenarios is not None:
            T = scenarios.shape[1]
        elif covariance is not None:
            Sigma = np.zeros((n, n))
            for i, ci in enumerate(codes):
                for j, cj in enumerate(codes):
                    Sigma[i, j] = covariance.get(ci, {}).get(cj, 0.0)
            scenarios = self._generate_scenarios(mu, Sigma, n_scenarios=1000)
        else:
            scenarios = np.random.normal(mu[:, None], 0.1, size=(n, 1000))

        try:
            from scipy.optimize import linprog
        except ImportError:
            return self._fallback_optimize(expected_returns)

        T = scenarios.shape[1]

        c = np.zeros(n + T + 1)
        c[n] = 1.0
        c[n + 1:] = 1 / ((1 - self.alpha) * T)

        A_eq = np.zeros((n, n + T + 1))
        A_eq[:, :n] = np.eye(n)
        A_eq[:, -1] = -1
        b_eq = np.ones(n)

        A_ub = np.zeros((T, n + T + 1))
        for t in range(T):
            A_ub[t, :n] = scenarios[:, t]
            A_ub[t, n + t] = 1
        b_ub = np.zeros(T)

        bounds = [(min_weight, max_weight)] * n + [(None, None)] * T + [(None, None)]

        constraints = []
        if max_cvar is not None:
            cvar_row = np.zeros(n + T + 1)
            cvar_row[n] = 1
            for t in range(T):
                cvar_row[n + 1 + t] = 1 / ((1 - self.alpha) * T)
            constraints.append({"type": "ub", "fun": lambda x, r=cvar_row: max_cvar - r @ x})

        if min_return is not None:
            ret_row = np.zeros(n + T + 1)
            ret_row[:n] = mu
            constraints.append({"type": "lb", "fun": lambda x, r=ret_row: r @ x - min_return})

        result = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")

        if result.success:
            w = result.x[:n]
            var = -result.x[n]
            cvar = var + result.x[n + 1:].sum() / ((1 - self.alpha) * T)
            exp_ret = float(w @ mu)
            return CVaRResult(
                weights={codes[i]: float(w[i]) for i in range(n)},
                cvar=float(cvar),
                var=float(var),
                expected_return=exp_ret,
            )

        return self._fallback_optimize(expected_returns)

    def _generate_scenarios(self, mu: np.ndarray, Sigma: np.ndarray, n_scenarios: int = 1000) -> np.ndarray:
        n = len(mu)
        try:
            L = np.linalg.cholesky(Sigma + np.eye(n) * 1e-6)
            z = np.random.normal(0, 1, size=(n, n_scenarios))
            return mu[:, None] + L @ z
        except np.linalg.LinAlgError:
            return np.random.normal(mu[:, None], 0.1, size=(n, n_scenarios))

    def _fallback_optimize(self, expected_returns: dict[str, float]) -> CVaRResult:
        codes = list(expected_returns.keys())
        total = sum(expected_returns.values()) or 1.0
        weights = {c: round(expected_returns[c] / total, 4) for c in codes}
        return CVaRResult(
            weights=weights,
            cvar=0.0,
            var=0.0,
            expected_return=sum(expected_returns.values()) / len(codes) if codes else 0.0,
        )

    def max_sharpe_cvar(
        self,
        expected_returns: dict[str, float],
        covariance: dict[str, dict[str, float]],
        min_weight: float = 0.0,
        max_weight: float = 1.0,
    ) -> CVaRResult:
        best_result = None
        best_sharpe = -np.inf

        for target_ret in np.linspace(min(expected_returns.values()), max(expected_returns.values()), 20):
            result = self.optimize(
                expected_returns=expected_returns,
                covariance=covariance,
                min_return=target_ret,
                min_weight=min_weight,
                max_weight=max_weight,
            )
            if result.expected_return > 0:
                sharpe = (result.expected_return - self.risk_free_rate) / max(result.cvar, 1e-6)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_result = result

        return best_result or self.optimize(expected_returns, covariance, min_weight=min_weight, max_weight=max_weight)
