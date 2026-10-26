"""Tests for risk protection modules"""
from strategy_layer.risk.protections import CooldownPeriod, MaxDrawdownGuard, StoplossGuard, ProtectionResult


class TestCooldownPeriod:
    def test_unknown_code_can_trade(self):
        cd = CooldownPeriod(days=5)
        result = cd.check("000001", "2026-07-24")
        assert result.can_trade is True

    def test_cooldown_blocks_trade(self):
        cd = CooldownPeriod(days=5)
        cd.record_sell("000001", "2026-07-20")
        result = cd.check("000001", "2026-07-21")
        assert result.can_trade is False

    def test_cooldown_expires(self):
        cd = CooldownPeriod(days=1)
        cd.record_sell("000001", "2026-07-20")
        result = cd.check("000001", "2026-07-27")
        assert result.can_trade is True

    def test_config_overrides_days(self):
        cd = CooldownPeriod(config={"strategy.risk.cooldown_days": 10}, days=5)
        assert cd.days == 10


class TestMaxDrawdownGuard:
    def test_normal_drawdown_allows_trading(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        result = guard.check(-0.05)
        assert result.can_trade is True

    def test_pauses_on_large_drawdown(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        result = guard.check(-0.20)
        assert result.can_trade is False

    def test_resumes_after_recovery(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        guard.check(-0.20)
        result = guard.check(-0.08)
        assert result.can_trade is True

    def test_stays_paused_if_not_recovered(self):
        guard = MaxDrawdownGuard(pause_at=-0.15, resume_at=-0.10)
        guard.check(-0.20)
        result = guard.check(-0.12)
        assert result.can_trade is False


class TestStoplossGuard:
    def test_below_max_allows_trading(self):
        guard = StoplossGuard(max_stoploss=3)
        result = guard.check("10:00")
        assert result.can_trade is True

    def test_pauses_after_max_stoploss(self):
        guard = StoplossGuard(max_stoploss=1, pause_minutes=60)
        guard.record_stoploss()
        result = guard.check("10:00")
        assert result.can_trade is False

    def test_records_multiple_stoplosses(self):
        guard = StoplossGuard(max_stoploss=3, pause_minutes=60)
        guard.record_stoploss()
        guard.record_stoploss()
        result = guard.check("10:00")
        assert result.can_trade is True
        guard.record_stoploss()
        result = guard.check("10:00")
        assert result.can_trade is False