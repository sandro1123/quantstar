"""Hierarchical Risk Parity — 层级风险平价"""
from dataclasses import dataclass

import numpy as np
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform


@dataclass
class HRPResult:
    weights: dict[str, float]
    clusters: list[list[str]]
    linkage_matrix: np.ndarray


class HRP:
    def __init__(self, linkage_method: str = "ward"):
        self.linkage_method = linkage_method

    def optimize(self, returns_history: dict[str, np.ndarray] = None) -> dict[str, float]:
        if not returns_history:
            return {}

        codes = list(returns_history.keys())
        n = len(codes)

        if n == 1:
            return {codes[0]: 1.0}

        returns_matrix = np.column_stack([returns_history[c] for c in codes])
        returns_matrix = np.nan_to_num(returns_matrix, nan=0.0)

        distance = self._compute_distance_matrix(returns_matrix)
        link = linkage(distance, method=self.linkage_method)

        sorted_indices = self._get_quasi_diagonal_ordering(link)
        weights = self._recursive_bisection(returns_matrix, sorted_indices)

        if weights.sum() > 0:
            weights = weights / weights.sum()

        return {codes[i]: float(weights[i]) for i in range(n)}

    def optimize_with_detail(self, returns_history: dict[str, np.ndarray]) -> HRPResult:
        if not returns_history:
            return HRPResult(weights={}, clusters=[], linkage_matrix=np.array([]))

        codes = list(returns_history.keys())
        n = len(codes)

        if n == 1:
            return HRPResult(
                weights={codes[0]: 1.0},
                clusters=[[codes[0]]],
                linkage_matrix=np.array([]),
            )

        returns_matrix = np.column_stack([returns_history[c] for c in codes])
        returns_matrix = np.nan_to_num(returns_matrix, nan=0.0)

        distance = self._compute_distance_matrix(returns_matrix)
        link = linkage(distance, method=self.linkage_method)

        sorted_indices = self._get_quasi_diagonal_ordering(link)
        weights = self._recursive_bisection(returns_matrix, sorted_indices)

        if weights.sum() > 0:
            weights = weights / weights.sum()

        weight_dict = {codes[i]: float(weights[i]) for i in range(n)}
        clusters = self._get_clusters(link, codes)
        return HRPResult(
            weights=weight_dict,
            clusters=clusters,
            linkage_matrix=link,
        )

    def _compute_distance_matrix(self, returns: np.ndarray) -> np.ndarray:
        corr = np.corrcoef(returns, rowvar=False)
        corr = np.clip(corr, -1 + 1e-8, 1 - 1e-8)
        distance = np.sqrt(0.5 * (1 - corr))
        np.fill_diagonal(distance, 0)
        return squareform(distance)

    def _get_quasi_diagonal_ordering(self, link: np.ndarray) -> list[int]:
        n = link.shape[0] + 1

        def traverse(node_idx: int) -> list[int]:
            if node_idx < n:
                return [node_idx]
            left, right = int(link[node_idx - n, 0]), int(link[node_idx - n, 1])
            return traverse(left) + traverse(right)

        root = n + link.shape[0] - 1
        return traverse(root)

    def _get_cluster_var(self, returns: np.ndarray, indices: list[int]) -> float:
        if len(indices) <= 1:
            return 0.0
        sub_returns = returns[:, indices]
        cov = np.cov(sub_returns, rowvar=False)
        if cov.ndim == 0:
            return float(cov)
        w = np.ones(len(indices)) / len(indices)
        return float(w @ cov @ w)

    def _recursive_bisection(self, returns: np.ndarray, indices: list[int]) -> np.ndarray:
        len(indices)

        def bisect(idx_list: list[int]) -> np.ndarray:
            if len(idx_list) <= 1:
                return np.ones(len(idx_list))

            mid = len(idx_list) // 2
            left_idx = idx_list[:mid]
            right_idx = idx_list[mid:]

            left_var = self._get_cluster_var(returns, left_idx)
            right_var = self._get_cluster_var(returns, right_idx)

            total_var = left_var + right_var
            alpha = 1 - left_var / total_var if total_var > 0 else 0.5
            alpha = np.clip(alpha, 0.01, 0.99)

            left_w = bisect(left_idx) * alpha
            right_w = bisect(right_idx) * (1 - alpha)

            return np.concatenate([left_w, right_w])

        return bisect(indices)

    def _get_clusters(self, link: np.ndarray, codes: list[str]) -> list[list[str]]:
        n = len(codes)
        if n <= 1:
            return [codes] if n == 1 else []

        cluster_map = {i: [i] for i in range(n)}
        next_id = n

        for row in link:
            left, right = int(row[0]), int(row[1])
            left_vals = cluster_map.get(left, [left])
            right_vals = cluster_map.get(right, [right])
            cluster_map[next_id] = left_vals + right_vals
            cluster_map.pop(left, None)
            cluster_map.pop(right, None)
            next_id += 1

        return [[codes[i] for i in cluster] for cluster in cluster_map.values() if len(cluster) > 0]
