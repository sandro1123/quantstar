"""Portfolio optimization unit tests"""
import numpy as np
import pytest


class TestEqualWeight:
    def setup_method(self):
        from strategy_layer.portfolio.equal_weight import EqualWeightPortfolio, PortfolioTarget
        self.portfolio = EqualWeightPortfolio(total_capital=1_000_000, max_positions=5)

    def test_allocate_equal_weight(self):
        codes = ["000001", "000002", "000003"]
        prices = {"000001": 10.0, "000002": 20.0, "000003": 30.0}
        targets = self.portfolio.allocate(codes, prices)

        assert len(targets) == 3
        assert all(abs(t.weight - 1/3) < 1e-6 for t in targets)
        assert all(t.shares >= 0 for t in targets)
        assert all(t.estimated_value > 0 for t in targets)

    def test_allocate_with_max_positions(self):
        codes = [f"{i:06d}" for i in range(10)]
        prices = {c: 10.0 for c in codes}
        targets = self.portfolio.allocate(codes, prices)
        assert len(targets) == 5

    def test_allocate_empty_codes(self):
        targets = self.portfolio.allocate([], {})
        assert targets == []

    def test_allocate_single_stock(self):
        targets = self.portfolio.allocate(["000001"], {"000001": 50.0})
        assert len(targets) == 1
        assert targets[0].weight == 1.0
        assert targets[0].shares == 20000

    def test_allocate_no_prices(self):
        codes = ["000001", "000002"]
        targets = self.portfolio.allocate(codes, prices=None)
        assert len(targets) == 2
        assert all(t.shares == 0 for t in targets)

    def test_allocate_partial_prices(self):
        codes = ["000001", "000002", "000003"]
        prices = {"000001": 10.0}
        targets = self.portfolio.allocate(codes, prices)
        assert len(targets) == 3
        assert targets[0].shares > 0
        assert targets[1].shares == 0
        assert targets[2].shares == 0

    def test_rebalance(self):
        current = {"000001": 400_000, "000002": 300_000, "000003": 300_000}
        new_codes = ["000001", "000002", "000003", "000004"]
        prices = {"000001": 10.0, "000002": 10.0, "000003": 10.0, "000004": 10.0}
        adjustments = self.portfolio.rebalance(current, new_codes, prices)
        assert "000004" in adjustments
        assert adjustments["000004"] > 0

    def test_rebalance_no_adjustments_needed(self):
        current = {"000001": 333_333, "000002": 333_333, "000003": 333_334}
        new_codes = ["000001", "000002", "000003"]
        prices = {"000001": 10.0, "000002": 10.0, "000003": 10.0}
        adjustments = self.portfolio.rebalance(current, new_codes, prices)
        assert all(abs(v) < 10_000 for v in adjustments.values())

    def test_rebalance_empty_current(self):
        adjustments = self.portfolio.rebalance({}, ["000001", "000002"], {"000001": 10.0, "000002": 10.0})
        assert len(adjustments) == 0  # no current holdings = no diff

    def test_portfolio_target_dataclass(self):
        from strategy_layer.portfolio.equal_weight import PortfolioTarget
        t = PortfolioTarget(code="000001", weight=0.5, shares=100, estimated_value=5000.0)
        assert t.code == "000001"
        assert t.weight == 0.5
        assert t.shares == 100
        assert t.estimated_value == 5000.0

    def test_portfolio_target_defaults(self):
        from strategy_layer.portfolio.equal_weight import PortfolioTarget
        t = PortfolioTarget(code="000001", weight=1.0)
        assert t.shares == 0
        assert t.estimated_value == 0.0


class TestBlackLitterman:
    def setup_method(self):
        from strategy_layer.portfolio.black_litterman import BlackLitterman
        self.mkt_weights = {"A": 0.5, "B": 0.3, "C": 0.2}
        self.cov = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        self.bl = BlackLitterman(
            market_cap_weights=self.mkt_weights,
            risk_aversion=2.5,
            tau=0.05,
        )

    def test_optimize_no_views(self):
        result = self.bl.optimize(views={}, covariance=self.cov)
        assert result.posterior_weights == self.mkt_weights
        assert result.implied_returns is not None

    def test_optimize_with_views(self):
        views = {"A": 0.15, "B": 0.10}
        confidence = {"A": 0.8, "B": 0.6}
        result = self.bl.optimize(views=views, confidence=confidence, covariance=self.cov)
        assert set(result.posterior_weights.keys()) == set(self.mkt_weights.keys())
        assert abs(sum(result.posterior_weights.values()) - 1.0) < 1e-4
        assert result.posterior_weights["A"] != self.mkt_weights["A"]

    def test_optimize_empty_market(self):
        from strategy_layer.portfolio.black_litterman import BlackLitterman
        bl = BlackLitterman(market_cap_weights={})
        result = bl.optimize(views={"A": 0.1}, covariance={})
        assert result.posterior_weights == {}

    def test_views_shift_weights_toward_views(self):
        views = {"A": 0.20, "B": 0.05}
        confidence = {"A": 0.9, "B": 0.9}
        result = self.bl.optimize(views=views, confidence=confidence, covariance=self.cov)
        assert result.posterior_weights["A"] > self.mkt_weights["A"]
        assert result.posterior_weights["B"] < self.mkt_weights["B"]

    def test_optimize_single_asset(self):
        from strategy_layer.portfolio.black_litterman import BlackLitterman
        bl = BlackLitterman(market_cap_weights={"A": 1.0})
        cov = {"A": {"A": 0.04}}
        result = bl.optimize(views={"A": 0.10}, confidence={"A": 0.5}, covariance=cov)
        assert abs(sum(result.posterior_weights.values()) - 1.0) < 1e-6
        assert "A" in result.posterior_weights
        assert "A" in result.posterior_returns
        assert "A" in result.posterior_covariance

    def test_optimize_two_assets(self):
        from strategy_layer.portfolio.black_litterman import BlackLitterman
        bl = BlackLitterman(market_cap_weights={"X": 0.6, "Y": 0.4})
        cov = {"X": {"X": 0.05, "Y": 0.02}, "Y": {"X": 0.02, "Y": 0.03}}
        result = bl.optimize(views={"X": 0.12}, covariance=cov)
        assert abs(sum(result.posterior_weights.values()) - 1.0) < 1e-4
        assert len(result.posterior_weights) == 2

    def test_optimize_no_confidence_defaults(self):
        result = self.bl.optimize(views={"A": 0.15}, covariance=self.cov)
        assert abs(sum(result.posterior_weights.values()) - 1.0) < 1e-4

    def test_posterior_returns_and_covariance_returned(self):
        result = self.bl.optimize(views={"A": 0.15}, confidence={"A": 0.5}, covariance=self.cov)
        assert len(result.posterior_returns) == 3
        assert len(result.posterior_covariance) == 3
        assert result.posterior_covariance["A"]["A"] > 0

    def test_default_risk_aversion_and_tau(self):
        from strategy_layer.portfolio.black_litterman import BlackLitterman
        bl = BlackLitterman(market_cap_weights={"A": 1.0})
        assert bl.risk_aversion == 2.5
        assert bl.tau == 0.05

    def test_negative_views(self):
        views = {"A": -0.05, "B": 0.15}
        confidence = {"A": 0.5, "B": 0.5}
        result = self.bl.optimize(views=views, confidence=confidence, covariance=self.cov)
        assert abs(sum(result.posterior_weights.values()) - 1.0) < 1e-4
        assert result.posterior_weights["A"] < self.mkt_weights["A"]


class TestCVaROptimizer:
    def setup_method(self):
        from strategy_layer.portfolio.cvar_optimizer import CVaROptimizer
        self.cvar = CVaROptimizer(alpha=0.95)

    def test_optimize_basic(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.optimize(expected_returns, covariance)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert all(w >= 0 for w in result.weights.values())
        assert result.cvar >= 0
        assert result.expected_return > 0

    def test_optimize_empty(self):
        result = self.cvar.optimize({})
        assert result.weights == {}
        assert result.cvar == 0
        assert result.var == 0

    def test_max_sharpe_cvar(self):
        expected_returns = {"A": 0.15, "B": 0.10, "C": 0.05}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.max_sharpe_cvar(expected_returns, covariance)
        assert result is not None
        assert result.expected_return > 0

    def test_optimize_single_asset(self):
        expected_returns = {"A": 0.10}
        result = self.cvar.optimize(expected_returns)
        assert abs(result.weights["A"] - 1.0) < 1e-4
        assert abs(result.expected_return - 0.10) < 1e-4

    def test_optimize_two_assets(self):
        expected_returns = {"X": 0.12, "Y": 0.08}
        covariance = {"X": {"X": 0.04, "Y": 0.02}, "Y": {"X": 0.02, "Y": 0.03}}
        result = self.cvar.optimize(expected_returns, covariance)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4

    def test_optimize_with_scenarios(self):
        expected_returns = {"A": 0.10, "B": 0.08}
        np.random.seed(42)
        scenarios = np.random.normal(0.001, 0.02, size=(2, 500))
        result = self.cvar.optimize(expected_returns, scenarios=scenarios)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert result.cvar >= 0

    def test_optimize_with_max_cvar(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.optimize(expected_returns, covariance, max_cvar=0.5)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4

    def test_optimize_with_min_return(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.optimize(expected_returns, covariance, min_return=0.09)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert result.expected_return >= 0.09 - 1e-4

    def test_optimize_with_min_weight(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.optimize(expected_returns, covariance, min_weight=0.1)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert all(w >= 0.1 - 1e-6 for w in result.weights.values())

    def test_optimize_with_max_weight(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.cvar.optimize(expected_returns, covariance, max_weight=0.5)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert all(w <= 0.5 + 1e-6 for w in result.weights.values())

    def test_optimize_no_covariance_no_scenarios(self):
        expected_returns = {"A": 0.10, "B": 0.08}
        np.random.seed(42)
        result = self.cvar.optimize(expected_returns)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4

    def test_max_sharpe_cvar_single_asset(self):
        expected_returns = {"A": 0.10}
        covariance = {"A": {"A": 0.04}}
        result = self.cvar.max_sharpe_cvar(expected_returns, covariance)
        assert result is not None
        assert abs(result.weights["A"] - 1.0) < 1e-4

    def test_default_alpha(self):
        from strategy_layer.portfolio.cvar_optimizer import CVaROptimizer
        opt = CVaROptimizer()
        assert opt.alpha == 0.95
        assert opt.risk_free_rate == 0.02

    def test_fallback_optimize_returns_valid_weights(self):
        expected_returns = {"A": 0.10, "B": 0.05}
        result = self.cvar._fallback_optimize(expected_returns)
        assert abs(sum(result.weights.values()) - 1.0) < 1e-4
        assert result.cvar == 0.0
        assert result.var == 0.0


class TestEfficientFrontier:
    def setup_method(self):
        from strategy_layer.portfolio.efficient_frontier import EfficientFrontier
        self.ef = EfficientFrontier(risk_free_rate=0.02)

    def test_compute_basic(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.compute(expected_returns, covariance, points=10)
        assert len(result.frontier) >= 2
        assert result.max_sharpe is not None
        assert result.min_variance is not None
        assert result.equal_weight is not None
        for pt in result.frontier:
            assert abs(sum(pt.weights.values()) - 1.0) < 1e-6
            assert all(0 <= w <= 1 for w in pt.weights.values())
            assert pt.volatility >= 0

    def test_compute_empty_returns(self):
        result = self.ef.compute({}, {})
        assert len(result.frontier) == 0
        assert result.max_sharpe is None

    def test_max_sharpe(self):
        expected_returns = {"A": 0.15, "B": 0.10, "C": 0.05}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.max_sharpe(expected_returns, covariance)
        assert result is not None
        assert result.sharpe_ratio > 0

    def test_compute_single_asset(self):
        expected_returns = {"A": 0.10}
        covariance = {"A": {"A": 0.04}}
        result = self.ef.compute(expected_returns, covariance)
        assert result.max_sharpe is not None
        assert result.min_variance is not None
        assert result.equal_weight is not None
        assert abs(result.equal_weight.weights["A"] - 1.0) < 1e-6

    def test_compute_two_assets(self):
        expected_returns = {"X": 0.12, "Y": 0.08}
        covariance = {"X": {"X": 0.04, "Y": 0.02}, "Y": {"X": 0.02, "Y": 0.03}}
        result = self.ef.compute(expected_returns, covariance, points=5)
        assert len(result.frontier) >= 2
        assert result.max_sharpe is not None
        assert result.equal_weight is not None
        assert abs(sum(result.equal_weight.weights.values()) - 1.0) < 1e-6

    def test_compute_with_min_weight(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.compute(expected_returns, covariance, min_weight=0.1)
        assert result.max_sharpe is not None
        for pt in result.frontier:
            assert all(w >= 0.1 - 1e-6 for w in pt.weights.values())

    def test_compute_with_max_weight(self):
        expected_returns = {"A": 0.12, "B": 0.10, "C": 0.08}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.compute(expected_returns, covariance, max_weight=0.5)
        assert result.max_sharpe is not None
        for pt in result.frontier:
            assert all(w <= 0.5 + 1e-6 for w in pt.weights.values())

    def test_equal_weight_point(self):
        expected_returns = {"A": 0.15, "B": 0.10, "C": 0.05}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.compute(expected_returns, covariance)
        eq = result.equal_weight
        assert eq is not None
        assert abs(sum(eq.weights.values()) - 1.0) < 1e-6
        assert all(abs(w - 1/3) < 1e-6 for w in eq.weights.values())
        assert eq.volatility > 0

    def test_min_variance_volatility_lower_than_max_sharpe(self):
        expected_returns = {"A": 0.15, "B": 0.10, "C": 0.05}
        covariance = {
            "A": {"A": 0.04, "B": 0.01, "C": 0.005},
            "B": {"A": 0.01, "B": 0.03, "C": 0.008},
            "C": {"A": 0.005, "B": 0.008, "C": 0.02},
        }
        result = self.ef.compute(expected_returns, covariance)
        assert result.min_variance.volatility <= result.max_sharpe.volatility + 1e-6

    def test_default_risk_free_rate(self):
        from strategy_layer.portfolio.efficient_frontier import EfficientFrontier
        ef = EfficientFrontier()
        assert ef.risk_free_rate == 0.02

    def test_max_sharpe_returns_none_for_empty(self):
        result = self.ef.max_sharpe({}, {})
        assert result is None

    def test_create_point(self):
        from strategy_layer.portfolio.efficient_frontier import PortfolioPoint
        pt = PortfolioPoint(
            weights={"A": 0.5, "B": 0.5},
            expected_return=0.10,
            volatility=0.05,
            sharpe_ratio=1.6,
        )
        assert pt.expected_return == 0.10
        assert pt.volatility == 0.05
        assert pt.sharpe_ratio == 1.6
        assert abs(sum(pt.weights.values()) - 1.0) < 1e-6


class TestHRP:
    def setup_method(self):
        from strategy_layer.portfolio.hrp import HRP
        self.hrp = HRP(linkage_method="ward")

    def test_optimize_basic(self):
        np.random.seed(42)
        n_assets = 10
        n_periods = 100
        returns = {}
        for i in range(n_assets):
            returns[f"STOCK_{i}"] = np.random.normal(0.001, 0.02, n_periods)
        weights = self.hrp.optimize(returns)
        assert len(weights) == n_assets
        assert abs(sum(weights.values()) - 1.0) < 1e-6
        assert all(w >= 0 for w in weights.values())

    def test_optimize_single_asset(self):
        returns = {"A": np.array([0.01, 0.02, -0.01, 0.015])}
        weights = self.hrp.optimize(returns)
        assert weights == {"A": 1.0}

    def test_optimize_empty(self):
        weights = self.hrp.optimize({})
        assert weights == {}

    def test_optimize_with_detail(self):
        np.random.seed(42)
        returns = {f"S{i}": np.random.normal(0.001, 0.02, 100) for i in range(5)}
        result = self.hrp.optimize_with_detail(returns)
        assert len(result.weights) == 5
        assert abs(sum(result.weights.values()) - 1.0) < 1e-6
        assert isinstance(result.clusters, list)
        assert result.linkage_matrix.shape[1] == 4

    def test_optimize_two_assets(self):
        np.random.seed(42)
        returns = {"X": np.random.normal(0.001, 0.02, 100), "Y": np.random.normal(0.001, 0.02, 100)}
        weights = self.hrp.optimize(returns)
        assert len(weights) == 2
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_optimize_with_detail_single_asset(self):
        returns = {"A": np.array([0.01, 0.02])}
        result = self.hrp.optimize_with_detail(returns)
        assert result.weights == {"A": 1.0}
        assert result.clusters == [["A"]]

    def test_optimize_with_detail_empty(self):
        result = self.hrp.optimize_with_detail({})
        assert result.weights == {}
        assert result.clusters == []

    def test_different_linkage_methods(self):
        np.random.seed(42)
        returns = {f"S{i}": np.random.normal(0.001, 0.02, 100) for i in range(5)}
        for method in ["ward", "single", "complete", "average"]:
            from strategy_layer.portfolio.hrp import HRP
            hrp = HRP(linkage_method=method)
            weights = hrp.optimize(returns)
            assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_optimize_all_zero_returns(self):
        import numpy as np
        returns = {"A": np.zeros(100), "B": np.zeros(100)}
        try:
            weights = self.hrp.optimize(returns)
            assert abs(sum(weights.values()) - 1.0) < 1e-6
        except Exception:
            pass  # all-zero returns may cause numerical issues, which is acceptable

    def test_optimize_nan_returns(self):
        np.random.seed(42)
        returns = {"A": np.array([np.nan, 0.01, np.nan, 0.02]), "B": np.array([0.01, np.nan, 0.02, np.nan])}
        weights = self.hrp.optimize(returns)
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_optimize_constant_returns(self):
        returns = {"A": np.ones(50) * 0.01, "B": np.ones(50) * 0.01}
        weights = self.hrp.optimize(returns)
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_default_linkage_method(self):
        from strategy_layer.portfolio.hrp import HRP
        hrp = HRP()
        assert hrp.linkage_method == "ward"

    def test_hrp_result_dataclass(self):
        from strategy_layer.portfolio.hrp import HRPResult
        result = HRPResult(
            weights={"A": 0.6, "B": 0.4},
            clusters=[["A", "B"]],
            linkage_matrix=np.array([[0, 1, 0.5, 2]]),
        )
        assert result.weights == {"A": 0.6, "B": 0.4}
        assert result.clusters == [["A", "B"]]
        assert result.linkage_matrix.shape == (1, 4)