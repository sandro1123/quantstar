from dataclasses import dataclass


@dataclass
class ProtectionResult:
    can_trade: bool = True
    reason: str = ""
    cooldown_until: str | None = None


class CooldownPeriod:
    """卖出后 N 个交易日禁止买入同一股票"""
    def __init__(self, config=None, days: int = 5):
        self.config = config or {}
        self.days = self.config.get("strategy.risk.cooldown_days", days)
        self._sold: dict[str, str] = {}  # code -> sell_date

    def record_sell(self, code: str, date: str):
        self._sold[code] = date

    def check(self, code: str, date: str) -> ProtectionResult:
        if code not in self._sold:
            return ProtectionResult()
        from datetime import datetime, timedelta
        sell_date = datetime.strptime(self._sold[code], "%Y-%m-%d")
        current = datetime.strptime(date, "%Y-%m-%d")
        trading_days = 0
        d = sell_date + timedelta(days=1)
        while d <= current:
            if d.weekday() < 5:
                trading_days += 1
            d += timedelta(days=1)
        if trading_days < self.days:
            cooldown_until = sell_date
            for _ in range(self.days):
                cooldown_until += timedelta(days=1)
                while cooldown_until.weekday() >= 5:
                    cooldown_until += timedelta(days=1)
            return ProtectionResult(
                can_trade=False,
                reason=f"Cooldown: sold on {self._sold[code]}, can buy again after {cooldown_until.strftime('%Y-%m-%d')}",
                cooldown_until=cooldown_until.strftime("%Y-%m-%d"),
            )
        return ProtectionResult()


class MaxDrawdownGuard:
    """当日最大回撤超过阈值时暂停交易"""
    def __init__(self, config=None, pause_at: float = -0.15, resume_at: float = -0.10):
        self.config = config or {}
        limits = self.config.get("strategy.risk.drawdown_limits", {"pause_at": pause_at, "resume_at": resume_at})
        self.pause_at = limits.get("pause_at", pause_at)
        self.resume_at = limits.get("resume_at", resume_at)
        self._paused = False

    def check(self, current_drawdown: float) -> ProtectionResult:
        if self._paused and current_drawdown >= self.resume_at:
            self._paused = False
            return ProtectionResult(can_trade=True, reason="Drawdown recovered")
        if not self._paused and current_drawdown <= self.pause_at:
            self._paused = True
            return ProtectionResult(can_trade=False, reason=f"MaxDrawdown: {current_drawdown:.1%} < {self.pause_at:.1%}")
        return ProtectionResult(can_trade=not self._paused)


class StoplossGuard:
    """当日触发多次止损后暂停交易"""
    def __init__(self, max_stoploss: int = 3, pause_minutes: int = 60):
        self.max_stoploss = max_stoploss
        self.pause_minutes = pause_minutes
        self._count = 0
        self._paused_until = None

    def record_stoploss(self):
        self._count += 1

    def check(self, current_time: str) -> ProtectionResult:
        if self._paused_until:
            from datetime import datetime
            now = datetime.strptime(current_time, "%H:%M")
            paused = datetime.strptime(self._paused_until, "%H:%M")
            if now >= paused:
                self._paused_until = None
                self._count = 0
                return ProtectionResult(can_trade=True, reason="Stoploss cooldown expired")
            return ProtectionResult(can_trade=False, reason=f"Stoploss guard active until {self._paused_until}")
        if self._count >= self.max_stoploss:
            from datetime import datetime, timedelta
            t = datetime.strptime(current_time, "%H:%M")
            self._paused_until = (t + timedelta(minutes=self.pause_minutes)).strftime("%H:%M")
            return ProtectionResult(can_trade=False, reason=f"Stoploss guard: {self._count} stoplosses, paused until {self._paused_until}")
        return ProtectionResult()
