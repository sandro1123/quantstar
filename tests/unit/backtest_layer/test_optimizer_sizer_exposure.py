"""Optimizer / Sizer / Exposure 单元测试"""
import pytest
from backtest_layer.core.optimizer import hrp_weights, max_sharpe_weights, equal_weight
from backtest_layer.core.sizer import PercentSizer, AllInSizer, FixedSizer
from backtest_layer.core.exposure import factor_exposure, style_exposure


# ── Optimizer ────────────────────────────────────────────────────────────────

class TestHRPWeights:
    def test_single_asset(self):
        w = hrp_weights({"A": [0.01, 0.02, -0.01]})
        assert w == {"A": 1.0}

    def test_two_assets(self):
        w = hrp_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]})
        assert set(w.keys()) == {"A", "B"}
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_identical_returns(self):
        w = hrp_weights({"A": [0.01, 0.02], "B": [0.01, 0.02]})
        assert set(w.keys()) == {"A", "B"}
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_empty_returns(self):
        w = hrp_weights({})
        assert w == {}

    def test_returns_type(self):
        w = hrp_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]})
        for v in w.values():
            assert isinstance(v, float)


class TestMaxSharpeWeights:
    def test_single_asset(self):
        w = max_sharpe_weights({"A": [0.01, 0.02, -0.01]})
        assert w == {"A": 1.0}

    def test_two_assets(self):
        w = max_sharpe_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]})
        assert set(w.keys()) == {"A", "B"}
        assert abs(sum(w.values()) - 1.0) < 1e-6
        for v in w.values():
            assert v >= 0

    def test_identical_returns(self):
        w = max_sharpe_weights({"A": [0.01, 0.02], "B": [0.01, 0.02]})
        assert set(w.keys()) == {"A", "B"}
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_empty_returns(self):
        w = max_sharpe_weights({})
        assert w == {}

    def test_custom_risk_free(self):
        w = max_sharpe_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]}, risk_free=0.05)
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_zero_risk_free(self):
        w = max_sharpe_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]}, risk_free=0.0)
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_high_risk_free_allocation(self):
        w = max_sharpe_weights({"A": [0.01, 0.02], "B": [0.03, 0.04]}, risk_free=0.30)
        assert abs(sum(w.values()) - 1.0) < 1e-6


class TestEqualWeight:
    def test_two_items(self):
        w = equal_weight(["A", "B"])
        assert w == {"A": 0.5, "B": 0.5}

    def test_three_items(self):
        w = equal_weight(["A", "B", "C"])
        assert w == {"A": 1/3, "B": 1/3, "C": 1/3}

    def test_single_item(self):
        w = equal_weight(["A"])
        assert w == {"A": 1.0}

    def test_empty_list(self):
        w = equal_weight([])
        assert w == {}

    def test_returns_type(self):
        w = equal_weight(["A", "B"])
        for v in w.values():
            assert isinstance(v, float)


# ── Sizer ────────────────────────────────────────────────────────────────────

class TestPercentSizer:
    def test_default_risk(self):
        sizer = PercentSizer()
        shares = sizer.size(100000, 50.0)
        assert isinstance(shares, int)
        assert shares >= 0

    def test_risk_per_trade_limits(self):
        sizer = PercentSizer(risk_per_trade=0.02, max_position_pct=0.25)
        shares = sizer.size(100000, 50.0, num_positions=1)
        target = min(100000 * 0.25, 100000 * 0.02 * 1)
        expected = int(target / 50.0 / 100) * 100
        assert shares == expected

    def test_max_position_pct_binds(self):
        sizer = PercentSizer(risk_per_trade=0.50, max_position_pct=0.10)
        shares = sizer.size(100000, 50.0, num_positions=1)
        target = 100000 * 0.10
        expected = int(target / 50.0 / 100) * 100
        assert shares == expected

    def test_zero_price(self):
        sizer = PercentSizer()
        assert sizer.size(100000, 0.0) == 0

    def test_negative_price(self):
        sizer = PercentSizer()
        assert sizer.size(100000, -10.0) == 0

    def test_zero_capital(self):
        sizer = PercentSizer()
        assert sizer.size(0, 50.0) == 0

    def test_negative_capital(self):
        sizer = PercentSizer()
        assert sizer.size(-1000, 50.0) == 0

    def test_small_capital_rounds_to_zero(self):
        sizer = PercentSizer(risk_per_trade=0.02, max_position_pct=0.25)
        assert sizer.size(100, 100.0) == 0

    def test_multiple_positions(self):
        sizer = PercentSizer(risk_per_trade=0.02)
        single = sizer.size(100000, 50.0, num_positions=1)
        multi = sizer.size(100000, 50.0, num_positions=5)
        assert multi >= single

    def test_custom_params(self):
        sizer = PercentSizer(risk_per_trade=0.01, max_position_pct=0.10)
        shares = sizer.size(50000, 100.0)
        target = min(50000 * 0.10, 50000 * 0.01 * 1)
        expected = int(target / 100.0 / 100) * 100
        assert shares == expected


class TestAllInSizer:
    def test_single_position(self):
        sizer = AllInSizer()
        shares = sizer.size(100000, 50.0, num_positions=1)
        expected = int(100000 / 50.0 / 100) * 100
        assert shares == expected

    def test_multiple_positions(self):
        sizer = AllInSizer()
        shares = sizer.size(100000, 50.0, num_positions=4)
        expected = int(25000 / 50.0 / 100) * 100
        assert shares == expected

    def test_zero_price(self):
        sizer = AllInSizer()
        assert sizer.size(100000, 0.0) == 0

    def test_negative_price(self):
        sizer = AllInSizer()
        assert sizer.size(100000, -10.0) == 0

    def test_zero_capital(self):
        sizer = AllInSizer()
        assert sizer.size(0, 50.0) == 0

    def test_negative_capital(self):
        sizer = AllInSizer()
        assert sizer.size(-1000, 50.0) == 0

    def test_small_capital_rounds_to_zero(self):
        sizer = AllInSizer()
        assert sizer.size(50, 100.0) == 0

    def test_return_type(self):
        sizer = AllInSizer()
        assert isinstance(sizer.size(100000, 50.0), int)

    def test_default_num_positions(self):
        sizer = AllInSizer()
        shares = sizer.size(100000, 50.0)
        expected = int(100000 / 50.0 / 100) * 100
        assert shares == expected


class TestFixedSizer:
    def test_default_shares(self):
        sizer = FixedSizer()
        assert sizer.size(100000, 50.0) == 1000

    def test_custom_shares(self):
        sizer = FixedSizer(shares=500)
        assert sizer.size(100000, 50.0) == 500

    def test_ignores_capital(self):
        sizer = FixedSizer(shares=100)
        assert sizer.size(0, 50.0) == 100

    def test_ignores_negative_price(self):
        sizer = FixedSizer(shares=100)
        assert sizer.size(100000, -10.0) == 100

    def test_ignores_num_positions(self):
        sizer = FixedSizer(shares=100)
        assert sizer.size(100000, 50.0, num_positions=10) == 100

    def test_return_type(self):
        sizer = FixedSizer()
        assert isinstance(sizer.size(100000, 50.0), int)

    def test_zero_shares(self):
        sizer = FixedSizer(shares=0)
        assert sizer.size(100000, 50.0) == 0


# ── Exposure ─────────────────────────────────────────────────────────────────

class TestFactorExposure:
    def test_full_output_keys(self):
        result = factor_exposure([0.01, 0.02, -0.01], [0.005, 0.01, -0.005])
        assert set(result.keys()) == {"alpha", "beta", "r_squared"}

    def test_beta_one(self):
        result = factor_exposure([0.01, 0.02], [0.01, 0.02])
        assert result["beta"] == 1.0

    def test_beta_zero(self):
        result = factor_exposure([0.01, 0.02], [0.0, 0.0])
        assert result["beta"] == 0.0

    def test_negative_beta(self):
        result = factor_exposure([0.01, -0.01], [0.01, -0.01])
        assert result["beta"] == 1.0

    def test_no_market_returns(self):
        result = factor_exposure([0.01, 0.02, -0.01])
        assert result["beta"] == 0.0
        assert result["r_squared"] == 0.0

    def test_market_returns_mismatch_length(self):
        result = factor_exposure([0.01, 0.02], [0.01])
        assert result["beta"] == 0.0

    def test_empty_returns(self):
        result = factor_exposure([], [0.01, 0.02])
        assert result == {"alpha": 0.0, "beta": 0.0, "r_squared": 0.0}

    def test_single_return(self):
        result = factor_exposure([0.01], [0.02])
        assert result == {"alpha": 0.0, "beta": 0.0, "r_squared": 0.0}

    def test_constant_returns_zero_variance(self):
        result = factor_exposure([0.01, 0.01, 0.01], [0.01, 0.02, 0.03])
        assert result["beta"] == 0.0

    def test_constant_market_zero_variance(self):
        result = factor_exposure([0.01, 0.02, 0.03], [0.01, 0.01, 0.01])
        assert result["beta"] == 0.0

    def test_perfect_positive_correlation(self):
        result = factor_exposure([0.01, 0.02, 0.03], [0.01, 0.02, 0.03])
        assert result["beta"] == 1.0
        assert result["r_squared"] == 1.0

    def test_all_zeros(self):
        result = factor_exposure([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        assert result["alpha"] == 0.0
        assert result["beta"] == 0.0
        assert result["r_squared"] == 0.0

    def test_longer_series(self):
        returns = [0.01, 0.02, -0.01, 0.005, 0.015, -0.005]
        mkt = [0.008, 0.015, -0.008, 0.004, 0.012, -0.004]
        result = factor_exposure(returns, mkt)
        assert 0 <= result["r_squared"] <= 1


class TestStyleExposure:
    def test_single_factor(self):
        result = style_exposure(
            [0.01, 0.02, -0.01],
            {"size": [0.005, 0.01, -0.005]}
        )
        assert "size" in result
        assert isinstance(result["size"], float)

    def test_multiple_factors(self):
        factors = {
            "size": [0.005, 0.01, -0.005],
            "value": [0.01, 0.0, -0.01],
            "momentum": [0.02, -0.01, 0.005],
        }
        result = style_exposure([0.01, 0.02, -0.01], factors)
        for k in factors:
            assert k in result

    def test_empty_returns(self):
        result = style_exposure([], {"size": [0.01, 0.02]})
        assert result == {}

    def test_single_return(self):
        result = style_exposure([0.01], {"size": [0.02]})
        assert result == {}

    def test_factor_length_mismatch(self):
        result = style_exposure(
            [0.01, 0.02, -0.01],
            {"size": [0.005, 0.01]}
        )
        assert result == {}

    def test_empty_factors(self):
        result = style_exposure([0.01, 0.02, -0.01], {})
        assert result == {}

    def test_perfect_correlation(self):
        result = style_exposure(
            [0.01, 0.02, 0.03],
            {"size": [0.01, 0.02, 0.03]}
        )
        assert result["size"] == 1.0

    def test_perfect_negative_correlation(self):
        result = style_exposure(
            [0.01, 0.02, 0.03],
            {"size": [-0.01, -0.02, -0.03]}
        )
        assert result["size"] == -1.0

    def test_zero_variance_returns(self):
        result = style_exposure(
            [0.01, 0.01, 0.01],
            {"size": [0.01, 0.02, 0.03]}
        )
        assert result["size"] == 0.0

    def test_zero_variance_factor(self):
        result = style_exposure(
            [0.01, 0.02, 0.03],
            {"size": [0.01, 0.01, 0.01]}
        )
        assert result["size"] == 0.0

    def test_returns_type(self):
        result = style_exposure(
            [0.01, 0.02, -0.01],
            {"size": [0.005, 0.01, -0.005]}
        )
        for v in result.values():
            assert isinstance(v, float)