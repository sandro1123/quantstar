"""backtest_layer/feedback/wfo_gate.py — WFO 门禁 (含 Sharpe/MDD/Trades 检查)

BT-11 ⑤ (B-13 口径统一): OOS Efficiency 全系统唯一口径 = walk_forward 聚合的
OOS/IS 收益比 (avg_oos_return / avg_is_return)。原 gate 内部自算
oos_sharpe/is_sharpe 夏普比且 min_oos_eff 默认 0 (等于不判) — 同名概念两种算法,
已删除。gate 不再自算效率比; 分级 (tier) 消费 evaluate() 透传的
oos_efficiency (调用方从 wf_result 传入)。
"""
from dataclasses import dataclass


@dataclass
class WFOEvaluation:
    passed: bool = False
    is_sharpe: float = 0.0
    oos_sharpe: float = 0.0
    degradation: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    reason: str = ""
    # BT-11 ⑤ (B-13): 唯一口径效率比 (OOS/IS 收益比), 由调用方传入透传, gate 不自算
    oos_efficiency: float | None = None


class WFOGate:
    def __init__(self, min_sharpe: float = 1.0, max_drawdown: float = 0.20,
                 min_trades: int = 100, min_excess_return: float = 0.0):
        self.min_sharpe = min_sharpe
        self.max_drawdown = max_drawdown
        self.min_trades = min_trades
        self.min_excess_return = min_excess_return

    def evaluate(self, is_report: dict, oos_report: dict,
                 oos_efficiency: float | None = None) -> WFOEvaluation:
        """五项判定 (Sharpe/MDD/trades/超额/退化)。

        BT-11 ⑤ (B-13): oos_efficiency 为可选透传参数 — 唯一口径是
        walk_forward 的 OOS/IS 收益比, gate 不再自算夏普比效率。
        """
        is_sharpe = is_report.get("sharpe", 0.0)
        oos_sharpe = oos_report.get("sharpe", 0.0)
        max_dd = oos_report.get("max_drawdown", 0.0)
        total_trades = oos_report.get("total_trades", 0)
        degradation = (is_sharpe - oos_sharpe) / is_sharpe if is_sharpe != 0 else 1.0

        reasons = []
        if oos_sharpe < self.min_sharpe:
            reasons.append(f"OOS Sharpe={oos_sharpe:.2f} < {self.min_sharpe}")
        if max_dd > self.max_drawdown:
            reasons.append(f"MDD={max_dd:.1%} > {self.max_drawdown:.0%}")
        if total_trades < self.min_trades:
            reasons.append(f"trades={total_trades} < {self.min_trades}")
        # BT-04: excess=None (基准不可用) 时不得 TypeError —
        # 下限>0 则 fail-closed 拦截 (无法验证即不放行), 下限=0 跳过 (不拿 0 假值冒充)
        excess = oos_report.get("excess_return")
        if excess is None:
            if self.min_excess_return > 0:
                reasons.append(f"excess_return=None (基准不可用) < {self.min_excess_return:.0%}")
        elif excess < self.min_excess_return:
            reasons.append(f"excess_return={excess:.2%} < {self.min_excess_return:.0%}")
        if degradation >= 0.5:
            reasons.append(f"degradation={degradation:.0%} >= 50%")

        passed = len(reasons) == 0
        return WFOEvaluation(
            passed=passed,
            is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe,
            degradation=round(degradation, 4),
            max_drawdown=round(max_dd, 4),
            total_trades=total_trades,
            reason="; ".join(reasons) if reasons else "",
            oos_efficiency=oos_efficiency,
        )

    def tier(self, evaluation: WFOEvaluation) -> str:
        """BT-05 (D-17) 三档动作分级 — 门禁判了门, 门后面要有路。

        - passed            → "deploy":        新权重写 active_factor_weights
                                               (自动部署档, config 开关默认关, record-only 验证期)
        - Efficiency 0.5-0.8 → "paper_trading": 权重写 paper_trading_weights (BT-12 纸交易消费)
        - Efficiency < 0.5   → "hold":          维持现有权重 + 告警

        BT-11 ⑤ (B-13): 分级依据 evaluation.oos_efficiency (唯一口径 =
        walk_forward 的 OOS/IS 收益比, evaluate() 透传)。效率比缺失时
        未通过门禁一律 hold — 宁可没有不可是假的, 不拿默认值分级。
        """
        if evaluation.passed:
            return "deploy"
        if evaluation.oos_efficiency is not None and evaluation.oos_efficiency >= 0.5:
            return "paper_trading"
        # G5 (2026-10-23): 「从未评估」与「评估了但不过」是两件事，必须分开。
        #
        # 实证: backtest_runs 24 条，wf_efficiency 非空 0 条 => 从未评估过。
        # 而告警却说「低于 0.5, 策略可能过拟合」—— 把「没测」说成「测了很差」。
        # 这是判例 #138 族（结论与事实不符会误导人工介入）。
        #
        # 保守行为不变（一律 hold，不拿默认值分级），只把**性质**透出去，
        # 让下游能给出诚实的告警措辞。
        if evaluation.oos_efficiency is None:
            return "hold_unevaluated"
        return "hold"
