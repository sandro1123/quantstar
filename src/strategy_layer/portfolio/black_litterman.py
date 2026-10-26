"""Black-Litterman 模型 — 观点融合优化器"""
from dataclasses import dataclass

import numpy as np


@dataclass
class BlackLittermanResult:
    posterior_weights: dict[str, float]
    posterior_returns: dict[str, float]
    posterior_covariance: dict[str, dict[str, float]]
    implied_returns: dict[str, float]


class BlackLitterman:
    def __init__(
        self,
        market_cap_weights: dict[str, float] = None,
        risk_aversion: float = 2.5,
        tau: float = 0.05,
    ):
        self.market_cap_weights = market_cap_weights or {}
        self.risk_aversion = risk_aversion
        self.tau = tau

    def optimize(
        self,
        views: dict[str, float],
        confidence: dict[str, float] = None,
        covariance: dict[str, dict[str, float]] = None,
    ) -> BlackLittermanResult:
        if not self.market_cap_weights or not covariance:
            return BlackLittermanResult(
                posterior_weights={},
                posterior_returns={},
                posterior_covariance={},
                implied_returns={},
            )

        codes = list(self.market_cap_weights.keys())
        n = len(codes)

        w_mkt = np.array([self.market_cap_weights[c] for c in codes])
        mu_implied = self._implied_equilibrium_returns(w_mkt, covariance)

        P, Q, Omega = self._build_view_matrices(codes, views, confidence, covariance)
        if P is None:
            return BlackLittermanResult(
                posterior_weights=self.market_cap_weights,
                posterior_returns=mu_implied,
                posterior_covariance=covariance,
                implied_returns=mu_implied,
            )

        Sigma = self._dict_to_matrix(codes, covariance)
        tau_Sigma = self.tau * Sigma

        M1 = np.linalg.inv(tau_Sigma)
        M2 = P.T @ np.linalg.inv(Omega) @ P
        M = M1 + M2

        mu_post = np.linalg.inv(M) @ (M1 @ mu_implied + P.T @ np.linalg.inv(Omega) @ Q)
        Sigma_post = np.linalg.inv(M)

        w_post = np.linalg.inv(self.risk_aversion * Sigma_post) @ mu_post
        w_post = w_post / w_post.sum() if w_post.sum() > 0 else w_mkt

        return BlackLittermanResult(
            posterior_weights={codes[i]: float(w_post[i]) for i in range(n)},
            posterior_returns={codes[i]: float(mu_post[i]) for i in range(n)},
            posterior_covariance=self._matrix_to_dict(codes, Sigma_post),
            implied_returns={codes[i]: float(mu_implied[i]) for i in range(n)},
        )

    def _implied_equilibrium_returns(
        self,
        w_mkt: np.ndarray,
        covariance: dict[str, dict[str, float]],
    ) -> np.ndarray:
        codes = list(self.market_cap_weights.keys())
        Sigma = self._dict_to_matrix(codes, covariance)
        return self.risk_aversion * Sigma @ w_mkt

    def _build_view_matrices(
        self,
        codes: list[str],
        views: dict[str, float],
        confidence: dict[str, float],
        covariance: dict[str, dict[str, float]],
    ) -> tuple:
        if not views:
            return None, None, None

        k = len(views)
        n = len(codes)

        P = np.zeros((k, n))
        Q = np.zeros(k)

        for i, (code, view) in enumerate(views.items()):
            if code in codes:
                P[i, codes.index(code)] = 1.0
                Q[i] = view

        if confidence is None:
            Omega = np.eye(k) * 0.01
        else:
            Omega = np.diag([confidence.get(c, 0.5) for c in views])
            Omega = np.diag(1.0 / np.maximum(Omega.diagonal(), 1e-6))

        return P, Q, Omega

    def _dict_to_matrix(self, codes: list[str], cov_dict: dict) -> np.ndarray:
        n = len(codes)
        mat = np.zeros((n, n))
        for i, ci in enumerate(codes):
            for j, cj in enumerate(codes):
                mat[i, j] = cov_dict.get(ci, {}).get(cj, 0.0)
        return mat

    def _matrix_to_dict(self, codes: list[str], mat: np.ndarray) -> dict[str, dict[str, float]]:
        len(codes)
        result = {}
        for i, ci in enumerate(codes):
            result[ci] = {}
            for j, cj in enumerate(codes):
                result[ci][cj] = float(mat[i, j])
        return result
