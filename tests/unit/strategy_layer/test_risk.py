"""交易保护机制单元测试"""
import pytest
from strategy_layer.risk.protections import ProtectionResult, CooldownPeriod, MaxDrawdownGuard, StoplossGuard
from strategy_layer.signals.generator import SignalGenerator
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.aggregation.aggregator import FactorDaily


class TestProtectionResult:
    def test_defaults(self):
        r = ProtectionResult()
        assert r.can_trade is True
        assert r.reason == ""
        assert r.cooldown_until is None

    def test_custom_values(self):
        r = ProtectionResult(can_trade=False, reason="test", cooldown_until="2026-07-20")
        assert r.can_trade is False
        assert r.reason == "test"
        assert r.cooldown_until == "2026-07-20"

    def test_partial_custom(self):
        r = ProtectionResult(can_trade=False)
        assert r.can_trade is False
        assert r.reason == ""
        assert r.cooldown_until is None


class TestCooldownPeriod:
    def test_allow_first_buy(self):
        cooldown = CooldownPeriod(days=5)
        result = cooldown.check("000001", "2026-07-17")
        assert result.can_trade is True

    def test_block_within_cooldown(self):
        cooldown = CooldownPeriod(days=5)
        cooldown.record_sell("000001", "2026-07-10")
        result = cooldown.check("000001", "2026-07-12")
        assert result.can_trade is False
        assert "Cooldown" in result.reason

    def test_allow_after_cooldown(self):
        cooldown = CooldownPeriod(days=5)
        cooldown.record_sell("000001", "2026-07-10")  # Friday
        # 5 trading days: Mon(13), Tue(14), Wed(15), Thu(16), Fri(17)
        result = cooldown.check("000001", "2026-07-17")
        assert result.can_trade is True

    def test_cooldown_until_date(self):
        cooldown = CooldownPeriod(days=5)
        cooldown.record_sell("000001", "2026-07-10")  # Friday
        result = cooldown.check("000001", "2026-07-12")
        assert result.cooldown_until == "2026-07-17"  # 5 trading days from Friday

    def test_multiple_stocks_independent(self):
        cooldown = CooldownPeriod(days=5)
        cooldown.record_sell("000001", "2026-07-10")
        cooldown.record_sell("000002", "2026-07-15")  # Wednesday
        assert cooldown.check("000001", "2026-07-12").can_trade is False
        # 000002: 5 trading days from Wed: Thu(16), Fri(17), Mon(20), Tue(21), Wed(22)
        assert cooldown.check("000002", "2026-07-22").can_trade is True

    def test_block_on_weekend_boundary(self):
        cooldown = CooldownPeriod(days=2)
        cooldown.record_sell("000001", "2026-07-10")  # Friday
        # 2 trading days: Mon(13), Tue(14)
        # After 2 trading days have passed, cooldown expires
        result = cooldown.check("000001", "2026-07-14")
        assert result.can_trade is True  # 2 trading days passed, not < 2

    def test_config_days_override(self):
        cooldown = CooldownPeriod(config={"strategy.risk.cooldown_days": 3})
        assert cooldown.days == 3
        cooldown.record_sell("000001", "2026-07-10")
        # 3 trading days: Mon(13), Tue(14), Wed(15)
        # After 3 trading days, cooldown expires
        assert cooldown.check("000001", "2026-07-15").can_trade is True  # 3 days passed, not < 3


class TestMaxDrawdownGuard:
    def test_allow_normal_drawdown(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        result = guard.check(-0.05)
        assert result.can_trade is True

    def test_pause_at_threshold(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        result = guard.check(-0.20)
        assert result.can_trade is False
        assert "MaxDrawdown" in result.reason

    def test_resume_after_recovery(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        guard.check(-0.20)
        result = guard.check(-0.08)
        assert result.can_trade is True

    def test_stay_paused_if_not_recovered(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        guard.check(-0.20)
        result = guard.check(-0.12)
        assert result.can_trade is False

    def test_default_params(self):
        guard = MaxDrawdownGuard()
        assert guard.pause_at == -0.15
        assert guard.resume_at == -0.10

    def test_config_limits_override(self):
        guard = MaxDrawdownGuard(config={
            "strategy.risk.drawdown_limits": {"pause_at": -0.08, "resume_at": -0.05},
        })
        assert guard.pause_at == -0.08
        assert guard.resume_at == -0.05
        assert guard.check(-0.09).can_trade is False
        assert guard.check(-0.04).can_trade is True

    def test_reason_format(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        result = guard.check(-0.20)
        assert "-20.0% < -15.0%" in result.reason


class TestStoplossGuard:
    def test_allow_below_max(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        assert guard.check("09:30").can_trade is True

    def test_trigger_after_max(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        guard.record_stoploss()
        guard.record_stoploss()
        guard.record_stoploss()
        result = guard.check("09:30")
        assert result.can_trade is False
        assert "Stoploss guard" in result.reason

    def test_reset_after_expiry(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        guard.record_stoploss()
        guard.record_stoploss()
        guard.record_stoploss()
        guard.check("09:30")
        result = guard.check("10:31")
        assert result.can_trade is True

    def test_still_paused_within_window(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        guard.record_stoploss()
        guard.record_stoploss()
        guard.record_stoploss()
        guard.check("09:30")
        result = guard.check("09:45")
        assert result.can_trade is False

    def test_reset_count_on_expiry(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        guard.record_stoploss()
        guard.record_stoploss()
        guard.record_stoploss()
        guard.check("09:30")
        guard.check("10:31")
        guard.check("10:32")
        assert guard.check("10:33").can_trade is True

    def test_counter_accumulates_before_pause(self):
        guard = StoplossGuard(max_stoploss=2, pause_minutes=60)
        assert guard.check("09:30").can_trade is True
        guard.record_stoploss()
        assert guard.check("09:31").can_trade is True
        guard.record_stoploss()
        result = guard.check("09:32")
        assert result.can_trade is False

    def test_config_params_override(self):
        guard = StoplossGuard(max_stoploss=1, pause_minutes=30)
        assert guard.max_stoploss == 1
        assert guard.pause_minutes == 30
        guard.record_stoploss()
        result = guard.check("09:30")
        assert result.can_trade is False
        # 30 minutes later
        result = guard.check("10:00")
        assert result.can_trade is True

    def test_expired_stoploss_reason(self):
        guard = StoplossGuard(max_stoploss=1, pause_minutes=30)
        guard.record_stoploss()
        guard.check("09:30")
        result = guard.check("10:00")
        assert result.can_trade is True
        assert "expired" in result.reason


class TestSignalGeneratorCooldown:
    def test_skip_buy_on_cooldown(self):
        gen = SignalGenerator()
        cooldown = CooldownPeriod(days=5)
        cooldown.record_sell("000001", "2026-07-10")
        selection = SelectionResult(
            date="2026-07-12",
            candidates=[
                FactorDaily(code="000001", date="2026-07-12", composite_score=1.5, rank=1),
                FactorDaily(code="000002", date="2026-07-12", composite_score=0.5, rank=2),
            ],
        )
        signals = gen.generate(selection, cooldown=cooldown)
        assert len(signals) == 1
        assert signals[0].code == "000002"

    def test_all_buy_when_no_cooldown(self):
        gen = SignalGenerator()
        cooldown = CooldownPeriod(days=5)
        selection = SelectionResult(
            date="2026-07-17",
            candidates=[
                FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1),
                FactorDaily(code="000002", date="2026-07-17", composite_score=0.5, rank=2),
            ],
        )
        signals = gen.generate(selection, cooldown=cooldown)
        assert len(signals) == 2