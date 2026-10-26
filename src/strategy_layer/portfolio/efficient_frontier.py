"""有效前沿分析 — Markowitz 均值-方差优化"""
from dataclasses import dataclass

import numpy as np


@dataclass
class PortfolioPoint:
    weights: dict[str, float]
    expected_return: float
    volatility: float
    sharpe_ratio: float


@dataclass
class EfficientFrontierResult:
    frontier: list[PortfolioPoint]
    max_sharpe: PortfolioPoint | None
    min_variance: PortfolioPoint | None
    equal_weight: PortfolioPoint | None


class EfficientFrontier:
    def __init__(self, risk_free_rate: float = 0.02):
        self.risk_free_rate = risk_free_rate

    def compute(
        self,
        expected_returns: dict[str, float],
        covariance: dict[str, dict[str, float]],
        points: int = 50,
        min_weight: float = 0.0,
        max_weight: float = 1.0,
    ) -> EfficientFrontierResult:
        if not expected_returns or not covariance:
            return EfficientFrontierResult(frontier=[], max_sharpe=None, min_variance=None, equal_weight=None)

        codes = list(expected_returns.keys())
        n = len(codes)

        mu = np.array([expected_returns[c] for c in codes], dtype=float)
        Sigma = np.zeros((n, n), dtype=float)
        for i, ci in enumerate(codes):
            for j, cj in enumerate(codes):
                Sigma[i, j] = covariance.get(ci, {}).get(cj, 0.0)

        if np.linalg.matrix_rank(Sigma) < n:
            Sigma = Sigma + np.eye(n) * 1e-6

        min_var_result = self._minimize_variance(mu, Sigma, None, min_weight, max_weight)
        min_var_point = self._create_point(codes, mu, Sigma, min_var_result) if min_var_result is not None else None

        max_sharpe_result = self._maximize_sharpe(mu, Sigma, min_weight, max_weight)
        max_sharpe_point = self._create_point(codes, mu, Sigma, max_sharpe_result) if max_sharpe_result is not None else None

        if min_var_point and max_sharpe_point:
            min_ret = min_var_point.expected_return
            max_ret = max_sharpe_point.expected_return
        else:
            min_ret = mu.min()
            max_ret = mu.max()

        target_returns = np.linspace(min_ret, max_ret, points)
        frontier = []

        for target_ret in target_returns:
            w = self._minimize_variance(mu, Sigma, target_ret, min_weight, max_weight)
            if w is not None:
                pt = self._create_point(codes, mu, Sigma, w)
                frontier.append(pt)

        eq_w = np.ones(n) / n
        eq_point = self._create_point(codes, mu, Sigma, eq_w)

        return EfficientFrontierResult(
            frontier=frontier,
            max_sharpe=max_sharpe_point,
            min_variance=min_var_point,
            equal_weight=eq_point,
        )

    def _minimize_variance(
        self,
        mu: np.ndarray,
        Sigma: np.ndarray,
        target_return: float | None,
        min_w: float,
        max_w: float,
    ) -> np.ndarray | None:
        n = len(mu)
        try:
            from scipy.optimize import minimize

            def objective(w):
                return w @ Sigma @ w

            constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
            if target_return is not None:
                constraints.append({"type": "eq", "fun": lambda w: w @ mu - target_return})

            bounds = [(min_w, max_w) for _ in range(n)]
            x0 = np.ones(n) / n

            result = minimize(objective, x0, bounds=bounds, constraints=constraints, method="SLSQP")
            if result.success:
                return result.x
        except ImportError:
            pass
        return None

    def _maximize_sharpe(
        self,
        mu: np.ndarray,
        Sigma: np.ndarray,
        min_w: float,
        max_w: float,
    ) -> np.ndarray | None:
        n = len(mu)
        try:
            from scipy.optimize import minimize

            def neg_sharpe(w):
                port_ret = w @ mu
                port_vol = np.sqrt(w @ Sigma @ w)
                return -(port_ret - self.risk_free_rate) / port_vol if port_vol > 0 else -1e6

            constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
            bounds = [(min_w, max_w) for _ in range(n)]
            x0 = np.ones(n) / n

            result = minimize(neg_sharpe, x0, bounds=bounds, constraints=constraints, method="SLSQP")
            if result.success:
                return result.x
        except ImportError:
            pass
        return None

    def _create_point(self, codes: list[str], mu: np.ndarray, Sigma: np.ndarray, w: np.ndarray) -> PortfolioPoint:
        port_ret = float(w @ mu)
        port_vol = float(np.sqrt(w @ Sigma @ w))
        sharpe = (port_ret - self.risk_free_rate) / port_vol if port_vol > 0 else 0.0
        return PortfolioPoint(
            weights={codes[i]: float(w[i]) for i in range(len(codes))},
            expected_return=port_ret,
            volatility=port_vol,
            sharpe_ratio=sharpe,
        )

    def max_sharpe(
        self,
        expected_returns: dict[str, float],
        covariance: dict[str, dict[str, float]],
        min_weight: float = 0.0,
        max_weight: float = 1.0,
    ) -> PortfolioPoint | None:
        result = self.compute(expected_returns, covariance, min_weight=min_weight, max_weight=max_weight)
        return result.max_sharpe
