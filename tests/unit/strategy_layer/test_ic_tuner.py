"""Tests for ICTuner IC computation"""
from strategy_layer.feedback.ic_tuner import ICTuner, FactorIC, ICSignificance


class TestICTuner:
    def test_compute_ic_returns_dict(self):
        tuner = ICTuner()
        factor_scores = {"momentum": {"000001": 0.5, "000002": 0.3, "000003": 0.1,
                                       "000004": -0.1, "000005": -0.3, "000006": -0.5,
                                       "000007": 0.2, "000008": 0.4, "000009": -0.2, "000010": 0.0}}
        forward_returns = {"000001": 0.05, "000002": 0.03, "000003": 0.01,
                          "000004": -0.01, "000005": -0.03, "000006": -0.05,
                          "000007": 0.02, "000008": 0.04, "000009": -0.02, "000010": 0.0}
        result = tuner.compute_ic(factor_scores, forward_returns)
        assert "momentum" in result
        assert isinstance(result["momentum"], float)

    def test_compute_ic_returns_zero_for_few_samples(self):
        tuner = ICTuner()
        result = tuner.compute_ic({"mom": {"000001": 0.5, "000002": 0.3}},
                                 {"mom": {"000001": 0.1, "000002": 0.2}})
        assert result["mom"] == 0.0

    def test_compute_ic_with_significance(self):
        tuner = ICTuner()
        scores = {f"{i:06d}": float(i) for i in range(20)}
        returns = {f"{i:06d}": float(i * 0.01) for i in range(20)}
        result = tuner.compute_ic_with_significance({"mom": scores}, returns)
        assert "mom" in result
        assert isinstance(result["mom"], ICSignificance)
        assert result["mom"].significant is not None

    def test_factor_ic_dataclass(self):
        fic = FactorIC(factor_name="momentum", ic_value=0.1, ir_value=0.5, trend="up", weight=0.05)
        assert fic.factor_name == "momentum"
        assert fic.ic_value == 0.1
        assert fic.trend == "up"

    def test_ic_significance_dataclass(self):
        sig = ICSignificance(ic=0.1, t_statistic=2.0, p_value=0.03, significant=True)
        assert sig.ic == 0.1
        assert sig.significant is True

    def test_set_sector_map(self):
        tuner = ICTuner()
        tuner.set_sector_map({"000001": "IT", "000002": "FIN"})
        assert tuner._sector_map["000001"] == "IT"