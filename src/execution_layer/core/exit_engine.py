"""出场引擎 — 6 层退出检查"""

from dataclasses import dataclass

from common.database import Database
from common.logging import get_logger
from execution_layer.core.reason_translator import format_value_saved_message, translate_reason

from agent_layer import build_request

logger = get_logger("execution.exit")


@dataclass
class ExitDecision:
    action: str
    reason_layer: int
    reason: str
    reason_user: str
    exit_low: float
    exit_high: float
    is_t1_delayed: bool = False
    value_saved_message: str | None = None


EXIT_LAYERS = {
    1: "硬止损/止盈",
    2: "ATR 止损",
    3: "移动止损",
    4: "技术面",
    5: "超时",
    6: "评分衰减",
}


class ExitEngine:
    def __init__(self, db: Database, stock_pool, agent_layer=None, config=None):
        self.db = db
        self.stock_pool = stock_pool
        self.agent_layer = agent_layer
        self.config = config
        cfg_get = (config.get if hasattr(config, 'get') else lambda k, d=None: d) if config else (lambda k, d=None: d)
        self.max_holding_days = cfg_get("execution.max_holding_days", 30)
        # layer4 技术退出最短持有期 (交易日, 由 entry_date 推算; holding_days 列是死列恒 0 不可用)
        self.min_holding_days = int(cfg_get("execution.min_holding_days", 3) or 3)
        # 持有交易日用无 db 近似日历 (周末+节假日表): 传 db 会全表扫 12.7M 行 daily_klines,
        # 盘中每持仓每轮检查都扫一遍不可接受; 3 日宽限期下近似误差可忽略
        from common.calendar import TradingCalendar
        self._calendar = TradingCalendar(db=None)
        self._action_record = None

    @property
    def action_record(self):
        if self._action_record is None:
            from execution_layer.core.action_record import ActionRecord
            self._action_record = ActionRecord(self.db)
        return self._action_record

    def check_exits(self, code: str, current_price: float, kline_data: list | None = None) -> ExitDecision | None:
        pool = self.stock_pool.get(code)
        if not pool or pool["status"] != "active":
            return None

        # T+1 检查：今天买入的不能今天卖出
        if pool.get("entry_date"):
            from common.utils import bj_date
            today = bj_date()
            entry_date = pool["entry_date"][:10]
            if entry_date == today:
                return None

        # Agent 退出复盘审核 — EX-03: 消费 AG-03 统一词表, 仅作参考不阻断 6 层检查
        # (原 verdict=="approve"/"reject" 字面量比较在词表统一后恒不匹配, 属死闸门;
        #  Agent 审核是参考非闸门, 推荐/警惕记参考信息, 观望/不推荐也不阻断, 一律继续 6 层)
        if self.agent_layer:
            try:
                from agent_layer.core.output_spec import normalize_verdict
                entry_price = pool.get("entry_price", 0) or 0
                # AG-WIRE-1: 显式 depth=quick — 出场检查是价格风控面, 5s 档足够
                req = build_request(task="exit_review", input={"code": code, "exit_price": current_price, "entry_price": entry_price}, consumer="execution")
                resp = self.agent_layer.analyze(req)
                verdict = normalize_verdict(resp.verdict)
                logger.info(f"Agent exit review for {code}: verdict={verdict} (参考, 不阻断 6 层检查)")
            except Exception as e:
                logger.warning(f"Agent exit review failed for {code}: {e} (诚实降级, 继续 6 层检查)")

        for layer in range(1, 7):
            decision = self._check_layer(layer, pool, current_price, kline_data)
            if decision is not None:
                decision.reason_user = translate_reason(decision.reason)
                decision.value_saved_message = format_value_saved_message(decision)
                # T+1 延迟执行：标记为 exit_pending，次日开盘执行
                # AUD-026: 统一走 mark_exit_trigger 正门 —— 旧实现用 update_status
                # 绕过, 只写 status+updated_at, 而 trigger_price/trigger_date/
                # exit_reason 全留 NULL (生产 22 只卡死仓位实证)。触发元数据缺失
                # 使 P0 的 T+1 真值回溯结算没有依据可用。
                # mark_exit_trigger 自带 active 幂等闸, 并自写 exit_check 留痕
                # (含 price), 故此处不再重复写 —— 层号已并入传入的 reason。
                self.stock_pool.mark_exit_trigger(
                    code, current_price, f"layer{layer}: {decision.reason}")
                decision.is_t1_delayed = True
                return decision
        return None

    def _check_layer(self, layer: int, pool: dict, current_price: float,
                     kline_data: list | None = None) -> ExitDecision | None:
        if layer == 1:
            return self._layer_1_hard_stop(pool, current_price)
        elif layer == 2:
            return self._layer_2_atr_stop(pool, current_price)
        elif layer == 3:
            return self._layer_3_trailing_stop(pool, current_price)
        elif layer == 4:
            return self._layer_4_technical(pool, current_price, kline_data)
        elif layer == 5:
            return self._layer_5_timeout(pool, current_price)
        elif layer == 6:
            return self._layer_6_score_decay(pool, current_price)
        return None

    def _layer_1_hard_stop(self, pool: dict, current_price: float) -> ExitDecision | None:
        stop_loss = pool.get("dynamic_stop_loss") or pool.get("stop_loss") or 0
        take_profit = pool.get("take_profit") or 0
        if stop_loss > 0 and current_price <= stop_loss:
            return ExitDecision(
                action="exit_pending", reason_layer=1,
                reason=f"硬止损: {current_price} <= {stop_loss}",
                reason_user=f"触发硬止损, 当前价 {current_price} 已跌破止损 {stop_loss}",
                exit_low=current_price * 0.98, exit_high=current_price,
            )
        if take_profit > 0 and current_price >= take_profit:
            return ExitDecision(
                action="exit_pending", reason_layer=1,
                reason=f"止盈: {current_price} >= {take_profit}",
                reason_user=f"触及止盈 {take_profit}, 当前价 {current_price}",
                exit_low=current_price, exit_high=current_price * 1.02,
            )
        return None

    def _layer_2_atr_stop(self, pool: dict, current_price: float) -> ExitDecision | None:
        atr_stop = pool.get("atr_stop") or pool.get("trailing_stop") or 0
        entry_price = pool.get("entry_price") or 0
        if atr_stop > 0 and entry_price > 0:
            atr_price = entry_price * (1 - 2 * atr_stop / 100)
            if current_price <= atr_price:
                return ExitDecision(
                    action="exit_pending", reason_layer=2,
                    reason=f"ATR止损: {current_price} <= ATR价 {atr_price:.2f}",
                    reason_user=f"触发ATR止损, 当前价 {current_price} 跌破 ATR 价位 {atr_price:.2f}",
                    exit_low=current_price * 0.98, exit_high=current_price,
                )
        return None

    def _layer_3_trailing_stop(self, pool: dict, current_price: float) -> ExitDecision | None:
        high = pool.get("highest_price") or 0
        entry_price = pool.get("entry_price") or 0
        # trailing_stop 列存储的是百分比值（如 8 表示 8%）
        trail_pct = (pool.get("trailing_stop") or 8) / 100.0
        if high > 0 and entry_price > 0:
            drawdown = (high - current_price) / high
            if drawdown > trail_pct and current_price > entry_price:
                return ExitDecision(
                    action="exit_pending", reason_layer=3,
                    reason=f"移动止损: 回撤{drawdown*100:.1f}% > {trail_pct*100:.0f}%",
                    reason_user=f"触发移动止损, 从高点{high}回撤{drawdown*100:.1f}%",
                    exit_low=current_price * 0.98, exit_high=current_price,
                )
        return None

    def _layer_4_technical(self, pool: dict, current_price: float,
                                kline_data: list | None = None) -> ExitDecision | None:
        """技术面退出：均线死叉/动量反转/RSI超买/MACD死叉"""
        # 最短持有期: 持仓 <N 交易日不触发技术退出 (2026-08-28 首日全灭事故辅修;
        # 只限 layer4, 硬止损/ATR 等风险层不受限); entry_date 缺失 fail-open 保持旧行为
        entry_date = (pool.get("entry_date") or "")[:10]
        if entry_date and self.min_holding_days > 0:
            try:
                from common.utils import bj_date
                days_held = self._calendar.trading_days_between(entry_date, bj_date())
            except Exception:
                days_held = self.min_holding_days  # 日历异常 fail-open
            if days_held < self.min_holding_days:
                return None
        if not kline_data or len(kline_data) < 20:
            return None
        try:
            closes = [k.close if hasattr(k, "close") else k.get("close", 0) for k in kline_data[-20:]]
            if len(closes) < 20:
                return None
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20
            # 均线死叉：ma5 < ma10 且 ma10 < ma20
            if ma5 < ma10 < ma20:
                return ExitDecision(
                    action="sell", reason_layer=4,
                    reason="均线空头排列",
                    reason_user="技术面转弱，均线死叉",
                    exit_low=round(current_price * 0.98, 2),
                    exit_high=round(current_price, 2),
                )
            # 动量反转：连续3日下跌
            if all(closes[-i] < closes[-i-1] for i in range(1, 4)):
                return ExitDecision(
                    action="sell", reason_layer=4,
                    reason="连续3日下跌",
                    reason_user="技术面动量反转",
                    exit_low=round(current_price * 0.98, 2),
                    exit_high=round(current_price, 2),
                )

            # RSI > 75 (超买)
            all_closes = [k.close if hasattr(k, "close") else k.get("close", 0) for k in kline_data]
            if len(all_closes) >= 15:
                rsi_closes = all_closes[-15:]
                gains = [max(0, rsi_closes[i] - rsi_closes[i-1]) for i in range(1, len(rsi_closes))]
                losses = [max(0, rsi_closes[i-1] - rsi_closes[i]) for i in range(1, len(rsi_closes))]
                avg_gain = sum(gains) / 14
                avg_loss = sum(losses) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                    if rsi > 75:
                        return ExitDecision(
                            action="sell", reason_layer=4,
                            reason=f"RSI超买: {rsi:.1f} > 75",
                            reason_user=f"技术指标RSI={rsi:.1f}进入超买区",
                            exit_low=round(current_price * 0.98, 2),
                            exit_high=round(current_price, 2),
                        )

            # MACD 死叉
            if len(all_closes) >= 35:
                macro_closes = all_closes[-35:]
                macd_values = []
                for i in range(25, len(macro_closes)):
                    chunk = macro_closes[:i+1]
                    ema12 = sum(chunk[-12:]) / 12
                    ema26 = sum(chunk[-26:]) / 26
                    macd_values.append(ema12 - ema26)
                if len(macd_values) >= 10:
                    current_macd = macd_values[-1]
                    current_signal = sum(macd_values[-9:]) / 9
                    prev_macd = macd_values[-2]
                    prev_signal = sum(macd_values[-10:-1]) / 9
                    if prev_macd > prev_signal and current_macd <= current_signal:
                        return ExitDecision(
                            action="sell", reason_layer=4,
                            reason="MACD死叉",
                            reason_user="技术指标MACD出现死叉信号",
                            exit_low=round(current_price * 0.98, 2),
                            exit_high=round(current_price, 2),
                        )
        except Exception:
            pass
        return None

    def _layer_5_timeout(self, pool: dict, current_price: float) -> ExitDecision | None:
        holding_days = pool.get("holding_days") or 0
        entry_price = pool.get("entry_price") or 0
        if holding_days >= self.max_holding_days and entry_price > 0:
            return ExitDecision(
                action="exit_pending", reason_layer=5,
                reason=f"超时: 持有{holding_days}天 >= {self.max_holding_days}天上限",
                reason_user=f"持仓超时{holding_days}天, 建议出场",
                exit_low=current_price * 0.98, exit_high=current_price * 1.02,
            )
        return None

    def _layer_6_score_decay(self, pool: dict, current_price: float) -> ExitDecision | None:
        current_score = pool.get("current_score") or 0
        initial_score = pool.get("initial_score") or 0
        if initial_score > 0 and current_score > 0:
            decay = (current_score - initial_score) / initial_score
            if decay < -0.3:
                return ExitDecision(
                    action="exit_pending", reason_layer=6,
                    reason=f"评分衰减: 从{initial_score}降至{current_score} ({decay*100:.0f}%)",
                    reason_user=f"评分从{initial_score}降至{current_score}, 跌幅{decay*100:.0f}%, 建议出场",
                    exit_low=current_price * 0.98, exit_high=current_price * 1.02,
                )
        return None
