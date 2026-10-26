"""ST-GOV 因子治理 RED→GREEN 夹具 (TDD) — 退役线真库回放 + 刹车三件。"""
import pytest

from strategy_layer.governance import (
    IR_GATE,
    RETIRE_CONSEC_DAYS,
    RETIRE_WEIGHT_FLOOR,
    apply_governance,
    compute_retired_factors,
    is_factor_frozen,
    layered_ic,
    market_regime_flag,
    significance_gate,
)


def _wrows(factor: str, weights: list[float], start_day=1):
    return [{"weight_date": f"2026-08-{i+1:02d}", "factor_name": factor, "weight": w}
            for i, w in enumerate(weights)]


class TestRetirementLine:
    def test_20_consecutive_dead_weights_retire(self):
        rows = _wrows("alpha_001", [0.005] * 25)
        m = compute_retired_factors(rows)
        assert m["alpha_001"]["retired"] is True
        assert m["alpha_001"]["consec_days"] == 25

    def test_19_days_not_yet(self):
        rows = _wrows("alpha_003", [0.005] * 19)
        m = compute_retired_factors(rows)
        assert m["alpha_003"]["retired"] is False
        assert m["alpha_003"]["consec_days"] == 19

    def test_ic_revival_resets_counter(self):
        # 复活语义: 连续 19 死 → 1 天回线上 → 归零重启
        rows = _wrows("alpha_005", [0.005] * 19 + [0.02] + [0.005] * 5)
        m = compute_retired_factors(rows)
        assert m["alpha_005"]["retired"] is False
        assert m["alpha_005"]["consec_days"] == 5

    def test_healthy_factor_never_retires(self):
        rows = _wrows("std20", [0.05] * 30)
        assert compute_retired_factors(rows)["std20"]["retired"] is False

    def test_frozen_check(self):
        rows = _wrows("alpha_001", [0.005] * 25)
        m = compute_retired_factors(rows)
        assert is_factor_frozen(m, "alpha_001") is True
        assert is_factor_frozen(m, "unknown_factor") is False

    def test_params_locked(self):
        # 治理参数单点定义锁 (用户拍板值)
        assert RETIRE_WEIGHT_FLOOR == 0.01
        assert RETIRE_CONSEC_DAYS == 20
        assert IR_GATE == 0.5


class TestLayeredIC:
    def test_full_data_weighted_blend(self):
        series = [0.01] * 60  # 常值 IC
        v = layered_ic(series)
        assert v == pytest.approx(0.01)

    def test_short_data_falls_back_to_fast(self):
        # 慢通道未满 → 只用快通道 (诚实降级)
        series = [0.02] * 20
        assert layered_ic(series) == pytest.approx(0.02)

    def test_insufficient_data_none(self):
        assert layered_ic([0.01] * 10) is None
        assert layered_ic([]) is None

    def test_fast_track_ignores_old_noise(self):
        # 旧 60 日大负 + 近 20 日回升 → 快通道抬权重, 不被远古拖死
        series = [-0.5] * 40 + [0.1] * 20
        v = layered_ic(series)
        fast = 0.1
        slow = (sum([-0.5] * 40) + sum([0.1] * 20)) / 60
        assert v == pytest.approx(0.4 * fast + 0.6 * slow)


class TestSignificanceGate:
    def test_gate_blocks_weak_ir(self):
        assert significance_gate(0.3) is False
        assert significance_gate(-0.3) is False
        assert significance_gate(None) is False

    def test_gate_passes_strong_ir(self):
        assert significance_gate(0.7) is True
        assert significance_gate(-0.7) is True


class TestMarketRegime:
    def test_median_badly_negative_flags_observation(self):
        ics = {f"alpha_{i:03d}": -0.10 for i in range(10)}
        assert market_regime_flag(ics) == "degraded_observation"

    def test_healthy_market_no_flag(self):
        ics = {f"alpha_{i:03d}": 0.02 for i in range(10)}
        assert market_regime_flag(ics) is None

    def test_mixed_market_median_decides(self):
        ics = {f"f{i}": v for i, v in enumerate([0.05] * 10 + [-0.08] * 12)}
        assert market_regime_flag(ics) == "degraded_observation"


class TestApplyGovernance:
    def test_regime_flag_freezes_all_adjustment(self):
        # 刹车3: 降档观察 → 权重原样冻结, 不普降
        weights = {"alpha_001": 0.05, "std20": 0.06}
        hist = [{"factor": "alpha_001", "ic": 0.08, "icir": 0.9}] * 30
        bad_market = {"alpha_001": -0.1, "std20": -0.1}
        out = apply_governance(weights, hist, factor_ics_today=bad_market)
        assert out["regime_flag"] == "degraded_observation"
        assert out["weights"] == weights  # 一字不动

    def test_weak_ir_skipped_by_gate(self):
        # 刹车2: |IR|<=0.5 → 不调权, 进 gate_skipped; 权重原样 (单因子归一后仍 1.0)
        weights = {"alpha_001": 0.05}
        hist = [{"factor": "alpha_001", "ic": -0.08, "icir": 0.2}] * 30
        out = apply_governance(weights, hist)
        assert "alpha_001" in out["gate_skipped"]
        # 只有 alpha_001 在权重表 → 归一化后必为 1.0 (值未被调过)
        assert out["weights"]["alpha_001"] == pytest.approx(1.0)

    def test_strong_negative_ir_cuts_weight(self):
        weights = {"alpha_001": 0.5, "std20": 0.5}
        hist = [{"factor": "alpha_001", "ic": -0.2, "icir": -0.9}] * 60
        out = apply_governance(weights, hist)
        assert out["weights"]["alpha_001"] < out["weights"]["std20"]

    def test_no_ic_history_no_change(self):
        weights = {"alpha_001": 0.05, "std20": 0.05}
        out = apply_governance(weights, [])
        total = sum(out["weights"].values())
        assert total == pytest.approx(1.0)
