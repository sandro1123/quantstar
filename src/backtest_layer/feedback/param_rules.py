"""backtest_layer/feedback/param_rules.py — 规则驱动参数调优 (BT-05 / 设计 §10.2)

AdaptiveParamTuner: 读真实推荐表现 (post_exit_tracking) + regime, 按规则引擎
产出买入门槛/止损参数建议。替代 ops _run_param_tuning 的"打日志假调参"。

与 param_tuner.py (Grid/Random/Bayesian 搜索) 的分工:
  - param_tuner: 在给定参数空间上做搜索寻优 (贵, 需回测引擎+数据)。
  - param_rules: 规则引擎, 由真实表现直接产出建议 (轻, 周级运行)。
两者都产出"真实参数建议"; 写库统一走 strategy_params 表 (config 门控, 建表报 GO)。

regime (牛/熊/震荡) 依赖 daily_regime 表 (BT-08, 策略层移交, 未就绪) —
表不存在时 regime=None, 跳过牛熊调整, 只跑胜率/盈亏比规则, 不造假 regime。
"""
from dataclasses import dataclass, field


@dataclass
class ParamSuggestion:
    buy_threshold: int = 65          # 买入门槛 (分数)
    stop_loss_pct: float = -0.08     # 止损幅度 (负数)
    adjustments: list = field(default_factory=list)  # 触发的规则说明
    n_samples: int = 0
    win_rate: float | None = None
    profit_loss_ratio: float | None = None
    regime: str | None = None

    def to_dict(self) -> dict:
        return {
            "buy_threshold": self.buy_threshold,
            "stop_loss_pct": self.stop_loss_pct,
            "adjustments": self.adjustments,
            "n_samples": self.n_samples,
            "win_rate": self.win_rate,
            "profit_loss_ratio": self.profit_loss_ratio,
            "regime": self.regime,
        }


def read_recommendation_performance(db, lookback_days: int = 30) -> dict:
    """从 post_exit_tracking 读真实推荐表现 → {win_rate, profit_loss_ratio, n_samples}。

    无表/无数据时返回空 dict (调用方据此跳过调参, 不造假表现)。
    """
    if db is None:
        return {}
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct FROM post_exit_tracking "
            "WHERE total_return_pct IS NOT NULL "
            "AND (exit_date >= date('now', ?) OR exit_date IS NULL)",
            (f"-{lookback_days} days",),
        ).fetchall()
    except Exception:
        return {}
    returns = [float(r[0]) for r in rows if r[0] is not None]
    if not returns:
        return {}
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    win_rate = len(wins) / len(returns)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else None
    return {"win_rate": win_rate, "profit_loss_ratio": profit_loss_ratio,
            "n_samples": len(returns)}


def read_regime(db, date: str) -> str | None:
    """读 daily_regime 当日状态 (BT-08 未就绪时表不存在 → 返回 None)。"""
    if db is None:
        return None
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT regime FROM daily_regime WHERE date = ?", (date,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


class AdaptiveParamTuner:
    """规则引擎 (设计 §10.2): 由真实表现产出参数建议, 不做搜索。"""

    def __init__(self, base_buy_threshold: int = 65, base_stop_loss_pct: float = -0.08):
        self.base_buy_threshold = base_buy_threshold
        self.base_stop_loss_pct = base_stop_loss_pct

    def suggest(self, performance: dict, regime: str | None = None) -> ParamSuggestion:
        s = ParamSuggestion(
            buy_threshold=self.base_buy_threshold,
            stop_loss_pct=self.base_stop_loss_pct,
            regime=regime,
        )
        if not performance:
            # 无真实表现 → 返回基准参数, 标注无数据, 不瞎调
            s.adjustments.append("no_performance_data: 维持基准参数")
            return s

        win_rate = performance.get("win_rate")
        plr = performance.get("profit_loss_ratio")
        s.n_samples = performance.get("n_samples", 0)
        s.win_rate = round(win_rate, 4) if win_rate is not None else None
        s.profit_loss_ratio = round(plr, 4) if plr is not None else None

        stop = s.stop_loss_pct  # 负数, 收紧=绝对值变小, 放开=绝对值变大

        if win_rate is not None:
            if win_rate < 0.35:
                s.buy_threshold += 10
                stop = stop * 0.85  # 收紧 15%
                s.adjustments.append(f"win_rate={win_rate:.0%}<35%: 买入门槛+10, 止损收紧15%")
            elif win_rate > 0.55:
                s.buy_threshold -= 5
                stop = stop * 1.10  # 放开 10%
                s.adjustments.append(f"win_rate={win_rate:.0%}>55%: 买入门槛-5, 止损放开10%")

        if plr is not None and plr < 1.0:
            stop = stop * 0.85  # 盈亏比差 → 再收紧 15%
            s.adjustments.append(f"profit_loss_ratio={plr:.2f}<1.0: 止损收紧15%")

        # regime 调整 (daily_regime 就绪后生效; None 时跳过, 不造假)
        if regime == "bear":
            stop = stop * 0.90
            s.adjustments.append("regime=bear: 整体收紧10%")
        elif regime == "bull":
            stop = stop * 1.05
            s.adjustments.append("regime=bull: 整体放开5%")

        s.stop_loss_pct = round(stop, 4)
        if not s.adjustments:
            s.adjustments.append("表现居中, 维持基准参数")
        return s
