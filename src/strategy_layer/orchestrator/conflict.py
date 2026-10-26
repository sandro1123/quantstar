from dataclasses import dataclass, field
from enum import Enum


class ConflictLevel(Enum):
    NONE = "none"
    SOFT = "soft"       # buy vs hold - keep buy
    HARD = "hard"       # buy vs sell - downgrade both


class SignalPriority(Enum):
    P0 = 0   # highest
    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4   # lowest


@dataclass
class TradeSignal:
    code: str
    action: str          # "buy" | "sell" | "hold"
    strength: float      # 0.0 ~ 1.0
    source: str = ""
    priority: SignalPriority = SignalPriority.P2


@dataclass
class ConflictReport:
    code: str
    level: ConflictLevel
    signals: list[TradeSignal] = field(default_factory=list)
    resolution: str = ""


class ConflictDetector:
    def detect(self, signals: list[TradeSignal]) -> list[ConflictReport]:
        """检测同一股票的信号冲突"""
        from collections import defaultdict
        by_code: dict[str, list[TradeSignal]] = defaultdict(list)
        for s in signals:
            by_code[s.code].append(s)

        reports = []
        for code, sigs in by_code.items():
            actions = {s.action for s in sigs}
            if len(actions) <= 1:
                continue
            if "buy" in actions and "sell" in actions:
                reports.append(ConflictReport(
                    code=code,
                    level=ConflictLevel.HARD,
                    signals=sigs,
                    resolution="downgrade_all",
                ))
            elif "buy" in actions and "hold" in actions:
                reports.append(ConflictReport(
                    code=code,
                    level=ConflictLevel.SOFT,
                    signals=sigs,
                    resolution="keep_buy",
                ))
        return reports


class ConflictResolver:
    def resolve(self, signals: list[TradeSignal], reports: list[ConflictReport]) -> list[TradeSignal]:
        """按冲突报告解决信号冲突"""
        conflict_codes = {r.code for r in reports}
        resolved = []
        for s in signals:
            if s.code in conflict_codes:
                report = next(r for r in reports if r.code == s.code)
                if report.resolution == "downgrade_all":
                    s.strength *= 0.3
                    resolved.append(s)
                elif report.resolution == "keep_buy":
                    if s.action == "buy":
                        resolved.append(s)
            else:
                resolved.append(s)
        return resolved
