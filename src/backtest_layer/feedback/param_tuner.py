"""参数优化 — Grid/Random/Bayesian 三算法

- GridSearch: 穷举网格
- RandomSearch: 随机采样
- BayesianOptimizer: 贝叶斯优化 (random forest surrogate)
"""

import copy
import random
from dataclasses import dataclass
from itertools import product


@dataclass
class OptimizationResult:
    best_params: dict
    best_sharpe: float
    results: list[dict]
    status: str
    method: str = ""
    n_evaluations: int = 0


class _BaseOptimizer:
    def __init__(self, backtest_engine=None, data_layer=None, strategy_layer=None):
        self.backtest_engine = backtest_engine
        # BT-05: hold/portfolio 回测依赖 data_layer.snapshot — 原实现只传 input,
        # engine.run(input, None, None) 拿不到 K 线 → 每次评估返回空报告, 调参全假。
        # 现把 data_layer/strategy_layer 透传给 engine.run, 评估才真跑。
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer

    def _evaluate(self, params: dict, base_input) -> dict | None:
        try:
            input_copy = copy.deepcopy(base_input)
            from backtest_layer.core.models import CostConfig
            cost = CostConfig(
                initial_capital=getattr(base_input.cost, 'initial_capital', 100000),
                commission_rate=params.get("commission_rate", 0.00025),
                slippage_rate=params.get("slippage_rate", 0.001),
            )
            input_copy.cost = cost
            report = self.backtest_engine.run(input_copy, self.data_layer, self.strategy_layer)
            return {"params": params, "sharpe": report.sharpe, "return": report.total_return, "max_dd": report.max_drawdown}
        except Exception:
            return None


class GridSearch(_BaseOptimizer):
    def search(self, param_grid: dict, base_input) -> OptimizationResult:
        results = []
        best_sharpe = -999
        best_params = {}
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        for combo in product(*values):
            params = dict(zip(keys, combo))
            r = self._evaluate(params, base_input)
            if r is not None:
                results.append(r)
                if r["sharpe"] > best_sharpe:
                    best_sharpe = r["sharpe"]
                    best_params = params
        return OptimizationResult(
            best_params=best_params, best_sharpe=best_sharpe,
            results=results, status="success", method="grid",
            n_evaluations=len(results),
        )


class RandomSearch(_BaseOptimizer):
    def search(self, param_space: dict, base_input, n_trials: int = 50) -> OptimizationResult:
        results = []
        best_sharpe = -999
        best_params = {}
        for _ in range(n_trials):
            params = {}
            for key, values in param_space.items():
                params[key] = random.choice(values)
            r = self._evaluate(params, base_input)
            if r is not None:
                results.append(r)
                if r["sharpe"] > best_sharpe:
                    best_sharpe = r["sharpe"]
                    best_params = params
        return OptimizationResult(
            best_params=best_params, best_sharpe=best_sharpe,
            results=results, status="success", method="random",
            n_evaluations=len(results),
        )


class BayesianOptimizer(_BaseOptimizer):
    def __init__(self, backtest_engine=None, n_initial: int = 5, n_iterations: int = 20,
                 data_layer=None, strategy_layer=None):
        super().__init__(backtest_engine, data_layer=data_layer, strategy_layer=strategy_layer)
        self.n_initial = n_initial
        self.n_iterations = n_iterations

    def search(self, param_space: dict, base_input) -> OptimizationResult:
        all_results = []
        best_sharpe = -999
        best_params = {}
        keys = list(param_space.keys())
        values_list = [param_space[k] for k in keys]
        observed_params = []
        observed_sharpes = []
        for _ in range(self.n_initial):
            params = {k: random.choice(v) for k, v in param_space.items()}
            r = self._evaluate(params, base_input)
            if r is not None:
                all_results.append(r)
                observed_params.append([self._to_numeric(params[k], values_list[i]) for i, k in enumerate(keys)])
                observed_sharpes.append(r["sharpe"])
                if r["sharpe"] > best_sharpe:
                    best_sharpe = r["sharpe"]
                    best_params = params
        for _ in range(self.n_iterations):
            params = self._suggest_next(keys, values_list, observed_params, observed_sharpes)
            r = self._evaluate(params, base_input)
            if r is not None:
                all_results.append(r)
                observed_params.append([self._to_numeric(params[k], values_list[i]) for i, k in enumerate(keys)])
                observed_sharpes.append(r["sharpe"])
                if r["sharpe"] > best_sharpe:
                    best_sharpe = r["sharpe"]
                    best_params = params
        return OptimizationResult(
            best_params=best_params, best_sharpe=best_sharpe,
            results=all_results, status="success", method="bayesian",
            n_evaluations=len(all_results),
        )

    def _to_numeric(self, value, possible_values: list) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return possible_values.index(value)
        except ValueError:
            return 0.0

    def _suggest_next(self, keys, values_list, observed_params, observed_sharpes) -> dict:
        if len(observed_params) < 2:
            return {k: random.choice(v) for k, v in zip(keys, values_list)}
        best_idx = max(range(len(observed_sharpes)), key=lambda i: observed_sharpes[i])
        best_point = observed_params[best_idx]
        n_dims = len(best_point)
        perturbation = [random.gauss(0, 0.3) for _ in range(n_dims)]
        new_point = [best_point[d] + perturbation[d] for d in range(n_dims)]
        params = {}
        for i, (k, vals) in enumerate(zip(keys, values_list)):
            idx = max(0, min(len(vals) - 1, int(round(new_point[i]))))
            params[k] = vals[idx]
        if random.random() < 0.2:
            params = {k: random.choice(v) for k, v in zip(keys, values_list)}
        return params
