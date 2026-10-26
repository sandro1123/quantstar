"""入场引擎 — 盘中触价检查"""

from dataclasses import dataclass

from common.database import Database
from common.logging import get_logger
from common.utils import bj_date

from agent_layer import build_request

logger = get_logger("execution.entry")


@dataclass
class EntryDecision:
    action: str
    price: float
    reason: str
    volume_suggestion: str | None = None


class EntryEngine:
    def __init__(self, db: Database, stock_pool, agent_layer=None, data_layer=None, config=None):
        self.db = db
        self.stock_pool = stock_pool
        self.agent_layer = agent_layer
        self.data_layer = data_layer
        self.config = config
        # R3/D1: 最近一次 check_entry 的闸门拦截原因 (放行/区间外为 None)。
        # 纯 stash 不重放门链 — 门链有副作用 (skip_entry 留痕/agent 状态更新),
        # 拦截原因在首次拦截时顺手记下, 供 entry_checker 透出给拦截蓝卡。
        self._block_reason: str | None = None
        # D1 续卡: 结构化拦截原因 {gate, detail} — gate=哪道闸, detail=具体判定
        # (宁 NULL 不可假值)。与 _block_reason 同点双写, 供 entry_blocks 落库/报告渲染。
        self._block_info: dict | None = None

    def _stash_block(self, gate: str, detail: str | None) -> None:
        """拦截 stash 统一入口: 结构化 info + 兼容文本 reason 一并落。"""
        self._block_reason = detail or gate
        self._block_info = {"gate": gate, "detail": detail}

    def _check_spike_defense(self, code: str, current_price: float) -> str | None:
        """追高防御：4 项检查，返回拒绝理由或 None"""
        if not self.data_layer:
            return None
        try:
            history = self.data_layer.get_history(code, bj_date(), 14)
            if not history or not history.klines or len(history.klines) < 5:
                return None
            closes = [k.close for k in history.klines[-5:]]
            avg_5 = sum(closes) / len(closes)
            # 1. 价格超过5日均价15%
            if current_price > avg_5 * 1.15:
                return "价格超过5日均价15%"
            # 2. 价格超过开盘价5% — 此检查独立于入场区间，即便开盘价在区间内也触发
            #    这是设计决策：防止任何时点的追高行为
            last = history.klines[-1]
            open_price = last.open if hasattr(last, 'open') else 0
            if open_price > 0 and current_price > open_price * 1.05:
                return "价格超过开盘价5%"
            # 3. 5日涨幅超过15%
            if len(closes) >= 5 and closes[-1] > 0:
                gain_5d = (closes[-1] - closes[0]) / closes[0]
                if gain_5d > 0.15:
                    return "5日涨幅超过15%"
            # 4. RSI > 70
            all_closes = [k.close for k in history.klines]
            if len(all_closes) >= 15:
                gains = [max(0, all_closes[i] - all_closes[i-1]) for i in range(1, 15)]
                losses = [max(0, all_closes[i-1] - all_closes[i]) for i in range(1, 15)]
                avg_gain = sum(gains) / 14 if gains else 0
                avg_loss = sum(losses) / 14 if losses else 0
                if avg_gain == 0 and avg_loss == 0:
                    rsi = 50.0
                elif avg_loss == 0:
                    rsi = 100.0
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                if rsi > 70:
                    return f"RSI > 70: {rsi:.1f}"
        except Exception:
            pass
        return None

    def check_entry(self, code: str, current_price: float, current_time: str) -> EntryDecision | None:
        self._block_reason = None  # R3/D1: 每次调用重置, 不残留上一笔
        self._block_info = None
        pool = self.stock_pool.get(code)
        if not pool or pool["status"] != "pending_entry":
            return None

        # S5 通电: 入场区间过期检查 — valid_until 有值且过期则拒单 (防按陈旧计划买入,
        # 05底稿项5: 字段有/规则有/没走到)。留痕 skip_entry; 状态不变 (清理归
        # expire/cleanup 职责)。valid_until 为空不拦 (诚实维持现状, 写入侧 S5 已补)。
        valid_until = pool.get("valid_until")
        if valid_until and bj_date() > str(valid_until)[:10]:
            reason = f"入场区间已过期 (valid_until={str(valid_until)[:10]}), 入场计划作废"
            logger.info(f"Entry expired for {code}: {reason}")
            try:
                self.stock_pool.action_record.write(
                    code=code, action_type="skip_entry", reason=reason,
                    source="execution")
            except Exception:  # noqa: BLE001 — 留痕失败不阻断拒单本身
                pass
            self._stash_block("valid_until", reason)  # D1 续卡
            return None

        # M2 通电: 风控三件套闸门 (冷却期/回撤熔断/连损日停) — 账本派生状态,
        # 拦截时闸门自行 skip_entry 留痕 + 熔断迁移飞书告警 (铁律#12)。
        from execution_layer.core.protection_gate import ProtectionGate
        gate = ProtectionGate(self.db, self.stock_pool, config=self.config)
        can_trade, prot_reason = gate.check(code, current_time)
        if not can_trade:
            logger.info(f"Protection gate blocked {code}: {prot_reason}")
            self._stash_block("protection", prot_reason)  # D1 续卡
            return None

        # Spike Defense 追高防御
        spike_reason = self._check_spike_defense(code, current_price)
        if spike_reason:
            logger.info(f"Spike defense blocked {code}: {spike_reason}")
            self._stash_block("spike_defense", f"追高防御: {spike_reason}")  # D1 续卡
            return None

        # Pre-execution constraint checks (G-E31)
        from execution_layer.core.constraint_checker import ConstraintChecker
        constraint_checker = ConstraintChecker(self.stock_pool, config=self.config)
        constraint_result = constraint_checker.check_entry(code, current_price)
        if not constraint_result.passed:
            logger.info(f"Entry constraints failed for {code}: {constraint_result.violations}")
            self._stash_block(
                "constraint",
                f"约束检查未过: {'; '.join(constraint_result.violations)}")  # D1 续卡
            return None

        # Agent 买入前审核
        if self.agent_layer:
            try:
                # AG-WIRE-1: 显式 depth=quick — 入场审核是价格风控面, 5s 档足够
                req = build_request(task="entry_check", input={"code": code, "entry_price": current_price}, consumer="execution")
                resp = self.agent_layer.analyze(req)
                if resp.verdict == "不推荐":  # AG-03: 统一词表
                    logger.info(f"Agent rejected entry for {code}: {resp.reasoning}")
                    self.stock_pool.update_status(code, "watch")
                    self._stash_block("agent_review", f"Agent 审核不推荐: {resp.reasoning}")  # D1 续卡
                    return None
            except Exception as e:
                logger.warning(f"Agent entry check failed for {code}: {e}")

        if pool.get("exit_date"):
            from datetime import datetime, timedelta
            fmt = "%Y-%m-%d"
            exit_dt = datetime.strptime(pool["exit_date"][:10], fmt)
            now_dt = datetime.strptime(bj_date(), fmt)
            min_holding = timedelta(days=1)
            if now_dt - exit_dt < min_holding:
                self._stash_block("min_holding", "距上次卖出未满 1 天 (最短持有期)")  # D1 续卡
                return None
        entry_low = pool["entry_low"] or 0
        entry_high = pool["entry_high"] or 0
        hour_min = current_time.split(":")[0] + ":" + current_time.split(":")[1]

        if "09:25" <= hour_min < "09:30":
            # EX-04: 浮点严格相等 (==) 在实盘竞价几乎不可能命中 → 死分支;
            # 改为 ±0.2% 容差 (abs(diff) <= entry_low * 0.002)
            if entry_low > 0 and abs(current_price - entry_low) <= entry_low * 0.002:
                return EntryDecision(
                    action="buy", price=current_price,
                    reason=f"竞合时段触价: {current_price} (容差±0.2% 内, 目标 {entry_low})",
                    volume_suggestion="normal",
                )
            return None
        elif "09:30" <= hour_min < "09:35":
            if entry_low <= current_price <= entry_low * 1.005:
                return EntryDecision(
                    action="buy", price=current_price,
                    reason=f"强共振入场: {current_price}",
                    volume_suggestion="试探",
                )
            return None
        elif "09:35" <= hour_min < "11:30" or "13:00" <= hour_min < "14:50":
            if entry_low <= current_price <= entry_high:
                return EntryDecision(
                    action="buy", price=current_price,
                    reason=f"触价: {current_price} 在区间 [{entry_low}, {entry_high}] 内",
                    volume_suggestion="normal",
                )
            return None
        elif "14:50" <= hour_min <= "15:00":
            expanded_high = entry_high * 1.02
            if entry_low <= current_price <= expanded_high:
                return EntryDecision(
                    action="buy", price=current_price,
                    reason=f"尾盘积极入场: {current_price} (范围扩展至 {expanded_high:.2f})",
                    volume_suggestion="aggressive",
                )
            return None

        return None
