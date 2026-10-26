"""backtest_layer/feedback/ic_feedback.py — IC 评估报告 (BT-05 / D-16 双制共存)

职责划分 (用户拍板 D-16, 两套都给真活, 不删除):
  - 策略层 ICTuner (strategy_layer/feedback/ic_tuner.py): 日级**调权** —
    compute_ic + update_weights_with_decay + 写 factor_weights。已接线, 三段式。
  - 回测层 ICFeedback (本文件): **只评估不调权** — 产出因子有效性报告,
    喂评估环 (BT-09 agent_eval / factor_eval) 与周报。

原实现的 adjust_weights (drop→0 / watch→0.5) 是"回测层也调权"的残留, 与策略层
ICTuner 职责重叠且无人消费 → 移除。评估结论 (recommendation) 保留, 供报告使用。
"""
from dataclasses import dataclass, field


@dataclass
class ICAnalysis:
    factor: str = ""
    mean_ic: float = 0.0
    ic_ir: float = 0.0
    n_obs: int = 0
    recommendation: str = "no_data"


@dataclass
class ICReport:
    """因子有效性报告 — 只评估不调权。喂评估环与周报。"""
    n_factors: int = 0
    n_obs: int = 0
    analyses: dict = field(default_factory=dict)   # {factor: ICAnalysis}
    strong: list = field(default_factory=list)
    weak_or_invalid: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_factors": self.n_factors,
            "n_obs": self.n_obs,
            "strong": self.strong,
            "weak_or_invalid": self.weak_or_invalid,
            "analyses": {
                f: {"mean_ic": a.mean_ic, "ic_ir": a.ic_ir,
                    "n_obs": a.n_obs, "recommendation": a.recommendation}
                for f, a in self.analyses.items()
            },
        }


class ICFeedback:
    """IC 评估器 — 只评估不调权 (D-16)。调权归策略层 ICTuner。"""

    def __init__(self, strategy_layer=None):
        # strategy_layer 仅用于未来读取因子元数据; 评估本身不依赖它。
        self.strategy_layer = strategy_layer

    def analyze(self, factor_name: str, ic_series: list[float]) -> ICAnalysis:
        if not ic_series:
            return ICAnalysis(factor=factor_name, mean_ic=0.0, ic_ir=0.0,
                              n_obs=0, recommendation="no_data")
        mean_ic = sum(ic_series) / len(ic_series)
        std_ic = (sum((x - mean_ic) ** 2 for x in ic_series) / len(ic_series)) ** 0.5
        ic_ir = mean_ic / std_ic if std_ic > 0 else 0.0
        if abs(mean_ic) > 0.02 and abs(ic_ir) > 0.5:
            recommendation = "keep"
        elif abs(mean_ic) > 0.01:
            recommendation = "watch"
        else:
            recommendation = "drop"
        return ICAnalysis(factor=factor_name, mean_ic=round(mean_ic, 4),
                          ic_ir=round(ic_ir, 4), n_obs=len(ic_series),
                          recommendation=recommendation)

    def generate_report(self, ic_series_by_factor: dict[str, list[float]]) -> ICReport:
        """对一批因子的 IC 序列产出有效性报告 (只评估, 不改任何权重)。"""
        report = ICReport(n_obs=0)
        if not ic_series_by_factor:
            return report
        for factor, series in ic_series_by_factor.items():
            analysis = self.analyze(factor, series)
            report.analyses[factor] = analysis
            if analysis.recommendation == "keep":
                report.strong.append(factor)
            elif analysis.recommendation in ("drop", "no_data"):
                report.weak_or_invalid.append(factor)
            if analysis.n_obs > report.n_obs:
                report.n_obs = analysis.n_obs
        report.n_factors = len(report.analyses)
        return report


def build_ic_report_from_db(db, lookback_days: int = 30) -> ICReport:
    """BT-05: 从 factor_ic 表读近 lookback_days 的每日 IC, 产出因子有效性报告。

    只读 + 只评估, 不写任何权重 (调权归策略层 ICTuner)。喂评估环与周报。
    无 db / 无表 / 无数据时返回空报告, 不造假。
    """
    if db is None:
        return ICReport()
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT factor_name, mean_ic FROM factor_ic "
            "WHERE calc_date >= date('now', ?) AND mean_ic IS NOT NULL "
            "ORDER BY calc_date ASC",
            (f"-{lookback_days} days",),
        ).fetchall()
    except Exception:
        return ICReport()
    series_by_factor: dict[str, list[float]] = {}
    for factor, ic in rows:
        if factor and ic is not None:
            series_by_factor.setdefault(factor, []).append(float(ic))
    return ICFeedback().generate_report(series_by_factor)
