"""多时间帧评分 — 日/周/月合成评分

实现状态 (ST-09, 2026-08-18): **排队** — run_daily 内的假合成段已拆除
(原调用不传周/月分数, fallback 使周/月分=日分数, 合成恒等于日分数, 审计 S-07)。
静默 fallback 一并移除: 缺真实周/月输入时显式报错, 不再拿日分数充数
(宁可没有, 不可是假的, 总原则 5)。
真实现启用条件: 周/月频因子计算管道建成 (决策账本 S-06, 排在盘中重建之后)。
"""
from dataclasses import dataclass, field

from common.calendar import TradingCalendar
from common.logging import get_logger

logger = get_logger("strategy.multiframe")

DEFAULT_TIMEFRAME_WEIGHTS = {"daily": 0.5, "weekly": 0.3, "monthly": 0.2}


@dataclass
class MultiTimeframeScore:
    code: str
    date: str
    daily_score: float = 0.0
    weekly_score: float = 0.0
    monthly_score: float = 0.0
    composite_score: float = 0.0
    details: dict = field(default_factory=dict)


class MultiTimeframeScorer:
    """多时间帧评分器 — 日/周/月合成"""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = weights or DEFAULT_TIMEFRAME_WEIGHTS

    def score(self, code: str, date: str,
              daily_score: float,
              weekly_scores: list[float] | None = None,
              monthly_scores: list[float] | None = None) -> MultiTimeframeScore:
        """计算多时间帧评分

        Args:
            code: 股票代码
            date: 日期
            daily_score: 日线评分
            weekly_scores: 最近5周的周评分列表
            monthly_scores: 最近3个月的月评分列表
        Returns:
            MultiTimeframeScore
        """
        # ST-09: 缺真实周/月输入时显式报错 — 原 fallback 用日分数充数 (假合成根因)
        if not weekly_scores:
            raise ValueError(f"weekly_scores required for {code}: 无真实周线分数, 拒绝用日分数充数")
        if not monthly_scores:
            raise ValueError(f"monthly_scores required for {code}: 无真实月线分数, 拒绝用日分数充数")
        weekly = sum(weekly_scores) / len(weekly_scores)
        monthly = sum(monthly_scores) / len(monthly_scores)

        composite = (
            self.weights.get("daily", 0.5) * daily_score +
            self.weights.get("weekly", 0.3) * weekly +
            self.weights.get("monthly", 0.2) * monthly
        )

        return MultiTimeframeScore(
            code=code,
            date=date,
            daily_score=round(daily_score, 4),
            weekly_score=round(weekly, 4),
            monthly_score=round(monthly, 4),
            composite_score=round(composite, 4),
            details={"weights": self.weights},
        )

    def score_batch(self, factor_daily: list,
                    daily_scores: dict[str, float],
                    weekly_scores: dict[str, list[float]] | None = None,
                    monthly_scores: dict[str, list[float]] | None = None) -> list[MultiTimeframeScore]:
        """批量计算多时间帧评分

        Args:
            factor_daily: FactorDaily 列表
            daily_scores: {code: daily_score}
            weekly_scores: {code: [w1, w2, w3, w4, w5]}
            monthly_scores: {code: [m1, m2, m3]}
        Returns:
            list[MultiTimeframeScore]
        """
        # ST-09: 周/月分数必须由调用方提供真实值; 缺失时 score() 显式报错 (不再 [日分] 充数)
        results = []
        for fd in factor_daily:
            ds = daily_scores.get(fd.code, fd.composite_score)
            ws = weekly_scores.get(fd.code) if weekly_scores else None
            ms = monthly_scores.get(fd.code) if monthly_scores else None
            results.append(self.score(fd.code, fd.date, ds, ws, ms))
        return results

    @staticmethod
    def get_weekly_boundaries(calendar: TradingCalendar, date: str, weeks: int = 5) -> list[str]:
        """获取最近N周的周一日期"""
        boundaries = []
        cal = calendar
        current = date
        for _ in range(weeks):
            dt = current
            while cal.is_trading_day(dt):
                from datetime import datetime
                d = datetime.strptime(dt, "%Y-%m-%d")
                if d.weekday() == 0:  # Monday
                    boundaries.append(dt)
                    break
                dt = cal.prev_trading_day(dt)
            current = cal.prev_trading_day(current)
        return boundaries

    @staticmethod
    def get_monthly_boundaries(calendar: TradingCalendar, date: str, months: int = 3) -> list[str]:
        """获取最近N个月的月初日期"""
        boundaries = []
        y = int(date[:4])
        m = int(date[5:7])
        for _ in range(months):
            m -= 1
            if m < 1:
                m = 12
                y -= 1
            first_day = f"{y}-{m:02d}-01"
            # 找到第一个交易日
            cal = calendar
            while not cal.is_trading_day(first_day):
                from datetime import datetime, timedelta
                d = datetime.strptime(first_day, "%Y-%m-%d") + timedelta(days=1)
                first_day = d.strftime("%Y-%m-%d")
            boundaries.append(first_day)
        return boundaries
