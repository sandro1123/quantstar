"""PaperBroker — 模拟交易执行，将 EntryDecision/ExitDecision 落地到 stock_pool"""
from dataclasses import dataclass


from common.logging import get_logger
from common.utils import bj_date, bj_now
from config_layer import cfg_get
from delivery_layer import DeliveryLayer, Message
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.constraint_checker import calculate_fees
from execution_layer.core.protection_gate import ProtectionGate  # Issue #8: paper trading 走同款风控闸门
from execution_layer.core.stock_pool import StockPool
# P1 合规措辞单一来源: 取值延迟到通知构造时 (模块顶 import 会经 intraday/__init__ 回环 monitor→paper_broker)

logger = get_logger("execution.paper_broker")

@dataclass
class PaperTradeResult:
    success: bool
    code: str = ""
    side: str = ""
    qty: int = 0
    price: float = 0.0
    gross_amount: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    total_fees: float = 0.0
    net_amount: float = 0.0
    return_pct: float = 0.0
    reason: str = ""


class PaperBroker:
    def __init__(self, db, stock_pool: StockPool, config=None, dry_run: bool = False):
        self.db = db
        self.stock_pool = stock_pool
        self.config = config  # Issue #8: 保存供 ProtectionGate 使用
        self.action_record = ActionRecord(db)
        self._initial_capital = cfg_get("execution.initial_capital", 300000.0)
        self._delivery = None
        self._delivery_ready = False
        self.dry_run = dry_run

    def _send_notification(self, title: str, body: str):
        if self.dry_run:
            logger.info(f"[DRY RUN] 通知: {title}\n{body}")
            return
        if not self._delivery_ready:
            try:
                from config_layer import get_config
                cfg = get_config()
                if not cfg._loaded:
                    cfg.load()
                self._delivery = DeliveryLayer(config=cfg)
                self._delivery.registry.mark_healthy("feishu_dm")
                self._delivery_ready = True
            except Exception:
                return
        try:
            content = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
                },
            }
            msg = Message(content=content, content_type="execution_alert", title=title)
            self._delivery.send(msg)
        except Exception as e:
            logger.warning(f"Notification failed: {e}")

    def execute_entry(self, code: str, decision_price: float, reason: str, pool_item: dict | None = None) -> bool:
        """执行买入 — pending_entry → active"""
        result = self.entry_with_fees(code, decision_price, reason, pool_item)
        return result.success

    def entry_with_fees(self, code: str, decision_price: float, reason: str, pool_item: dict | None = None) -> PaperTradeResult:
        """执行买入并返回费用明细的 PaperTradeResult"""
        try:
            pool = pool_item or self.stock_pool.get(code)
            if not pool or pool.get("status") != "pending_entry":
                return PaperTradeResult(success=False, code=code, side="entry", reason="invalid_status")

            entry_price = decision_price
            qty = self._calc_qty(entry_price)
            if qty <= 0:
                return PaperTradeResult(success=False, code=code, side="entry", price=entry_price, reason="zero_qty")

            # Issue #8: paper trading 必须穿过同款 ProtectionGate (生产主链路在 entry_engine
            # 里已通电, 纸交易此前绕闸直写 — 补齐后与 production 同一风控口径)。
            gate = ProtectionGate(self.db, self.stock_pool, config=self.config)
            can_trade, prot_reason = gate.check(code, bj_now()[:19])
            if not can_trade:
                logger.info(f"Paper entry blocked by protection gate for {code}: {prot_reason}")
                return PaperTradeResult(
                    success=False, code=code, side="entry", price=entry_price,
                    reason=f"protection_gate:{prot_reason}")

            amount = qty * entry_price
            fees = calculate_fees(entry_price, qty, is_buy=True)
            total_deduct = amount + fees["total"]
            available = self.stock_pool.get_available_cash(self._initial_capital)
            if total_deduct > available:
                logger.info(f"{code} 买入含费金额 {total_deduct:.0f} 超过可用资金 {available:.0f}")
                return PaperTradeResult(success=False, code=code, side="entry", qty=qty, price=entry_price,
                                        gross_amount=amount, commission=fees["commission"],
                                        transfer_fee=fees["transfer_fee"], total_fees=fees["total"],
                                        net_amount=total_deduct, reason="insufficient_cash")

            result = PaperTradeResult(success=True, code=code, side="entry", qty=qty, price=entry_price,
                                      gross_amount=amount, commission=fees["commission"],
                                      stamp_tax=0.0, transfer_fee=fees["transfer_fee"],
                                      total_fees=fees["total"], net_amount=total_deduct, reason=reason)

            if self.dry_run:
                logger.info(f"[DRY RUN] 买入: {code} {pool.get('name', code)} qty={qty} price={entry_price:.2f} amount={amount:.0f} fees={fees['total']:.2f}")
                self._send_notification(
                    f"触发买入-{code}",
                    f"实时价格: {decision_price:.2f}\n买入价格: {entry_price:.2f}\n买入数量: {qty} 股\n总金额: {amount:,.0f} 元\n费用: {fees['total']:.2f} 元\n理由: {reason}\n\n⚠️ 模拟运行，未实际执行"
                )
                return result

            now = bj_now()[:19]
            conn = self.db.get_connection()
            conn.execute("""
                UPDATE stock_pool SET
                    status = 'active', entry_price = ?, entry_date = ?,
                    position_qty = ?, last_price = ?, last_price_date = ?,
                    current_score = COALESCE(current_score, initial_score),
                    updated_at = ?
                WHERE code = ?
            """, (entry_price, bj_date(), qty, entry_price, bj_date(), now, code))
            conn.commit()

            self.action_record.write(code, "entry", reason, "execution",
                price=entry_price, new_value=total_deduct,
                action_detail=f"qty={qty},price={entry_price:.2f},fees={fees['total']:.2f}")
            logger.info(f"Paper entry: {code} {pool.get('name', code)} qty={qty} price={entry_price:.2f} fees={fees['total']:.2f}")

            available_after = self.stock_pool.get_available_cash(self._initial_capital)
            logger.info(f"可用资金: {available_after:.0f}")

            # P1 合规小卡 (2026-09-21, Kimi 审计): 生产分支通知补模拟标识 +
            # 股票名 + 免责行 —— 「总金额 N 元」零标识会让用户误读为真实资金
            # 动作 (合规红线); 措辞单一来源 (延迟 import 防循环)。
            from execution_layer.intraday.touch_card import COMPLIANCE_FOOTER
            _name = pool.get('name', code)
            self._send_notification(
                f"【模拟盘】触发买入-{code} {_name}",
                f"实时价格: {decision_price:.2f}\n买入价格: {entry_price:.2f}\n买入数量: {qty} 股\n总金额: {amount:,.0f} 元\n费用: {fees['total']:.2f} 元\n理由: {reason}\n\n📌 免责: {COMPLIANCE_FOOTER}"
            )
            return result
        except Exception as e:
            logger.warning(f"Paper entry failed for {code}: {e}")
            return PaperTradeResult(success=False, code=code, side="entry", reason=f"error:{e}")

    def execute_exit(self, code: str, decision_price: float, reason: str, pool_item: dict | None = None) -> bool:
        """执行卖出 — exit_pending → exited"""
        result = self.exit_with_fees(code, decision_price, reason, pool_item)
        return result.success

    def exit_with_fees(self, code: str, decision_price: float, reason: str, pool_item: dict | None = None) -> PaperTradeResult:
        """执行卖出并返回费用明细的 PaperTradeResult"""
        try:
            pool = pool_item or self.stock_pool.get(code)
            if not pool or pool.get("status") != "exit_pending":
                return PaperTradeResult(success=False, code=code, side="exit", reason="invalid_status")

            entry_price = pool.get("entry_price") or 0
            position_qty = pool.get("position_qty") or 0
            exit_price = decision_price
            holding_days = pool.get("holding_days") or 0
            gross = position_qty * exit_price
            fees = calculate_fees(exit_price, position_qty, is_buy=False)
            net_proceeds = fees["net_amount"]
            total_return = (net_proceeds - position_qty * entry_price) / (position_qty * entry_price) * 100 if entry_price > 0 and position_qty > 0 else 0

            result = PaperTradeResult(success=True, code=code, side="exit", qty=position_qty, price=exit_price,
                                      gross_amount=gross, commission=fees["commission"],
                                      stamp_tax=fees["stamp_tax"], transfer_fee=fees["transfer_fee"],
                                      total_fees=fees["total"], net_amount=net_proceeds,
                                      return_pct=total_return, reason=reason)

            if self.dry_run:
                logger.info(f"[DRY RUN] 卖出: {code} qty={position_qty} price={exit_price:.2f} net={net_proceeds:.0f} fees={fees['total']:.2f}")
                return_icon = "🟢" if total_return > 0 else "🔴"
                self._send_notification(
                    f"{return_icon} 触发卖出-{code}",
                    f"实时价格: {decision_price:.2f}\n卖出价格: {exit_price:.2f}\n卖出数量: {position_qty} 股\n总金额: {net_proceeds:,.0f} 元(含费)\n费用: {fees['total']:.2f} 元\n收益: {total_return:+.2f}%(净)\n持有: {holding_days} 天\n理由: {reason}\n\n⚠️ 模拟运行，未实际执行"
                )
                return result

            now = bj_now()[:19]
            conn = self.db.get_connection()
            with conn:
                cursor = conn.execute("""
                    UPDATE stock_pool SET
                        status = 'exited', exit_price = ?, exit_date = ?,
                        exit_reason = ?, exit_reason_detail = ?,
                        total_return_pct = ?, holding_days_actual = ?,
                        updated_at = ?
                    WHERE code = ? AND status = 'exit_pending'
                """, (exit_price, bj_date(), reason, reason, total_return, holding_days, now, code))
                if cursor.rowcount != 1:
                    return PaperTradeResult(success=False, code=code, side="exit", reason="invalid_status")
                self.action_record.write(code, "exit", reason, "execution",
                    price=exit_price, new_value=net_proceeds,
                    action_detail=f"qty={position_qty},price={exit_price:.2f},fees={fees['total']:.2f},return={total_return:.2f}%",
                    connection=conn)
            logger.info(f"Paper exit: {code} qty={position_qty} price={exit_price:.2f} return={total_return:.2f}% fees={fees['total']:.2f}")

            available_after = self.stock_pool.get_available_cash(self._initial_capital)
            logger.info(f"可用资金: {available_after:.0f}")

            return_icon = "🟢" if total_return > 0 else "🔴"
            # P1 合规小卡 (2026-09-21): 同买入面 — 模拟标识 + 股票名 + 免责行
            # (延迟 import 防循环, 与买入面同理由)。
            from execution_layer.intraday.touch_card import COMPLIANCE_FOOTER
            _pool_item = pool_item or {}
            _exit_name = _pool_item.get('name', code)
            self._send_notification(
                f"{return_icon} 【模拟盘】触发卖出-{code} {_exit_name}",
                f"实时价格: {decision_price:.2f}\n卖出价格: {exit_price:.2f}\n卖出数量: {position_qty} 股\n总金额: {net_proceeds:,.0f} 元(含费)\n费用: {fees['total']:.2f} 元\n收益: {total_return:+.2f}%(净)\n持有: {holding_days} 天\n理由: {reason}\n\n📌 免责: {COMPLIANCE_FOOTER}"
            )
            return result
        except Exception as e:
            logger.warning(f"Paper exit failed for {code}: {e}")
            return PaperTradeResult(success=False, code=code, side="exit", reason=f"error:{e}")

    def _calc_qty(self, price: float) -> int:
        if price <= 0:
            return 0
        available = self.stock_pool.get_available_cash(self._initial_capital)
        per_stock = available * 0.15
        qty = int(per_stock / price / 100) * 100
        return max(0, qty)
