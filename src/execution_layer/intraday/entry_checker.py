"""盘中入场检查 — 使用 daily_klines 最新收盘价（避免 snapshot_intraday N+1 超时）"""
from common.database import Database
from common.logging import get_logger

logger = get_logger("execution.intraday.entry")


class IntradayEntryChecker:
    def __init__(self, db: Database, stock_pool, entry_engine, data_layer=None):
        self.db = db
        self.stock_pool = stock_pool
        self.entry_engine = entry_engine
        self.data_layer = data_layer

    def _get_current_price(self, code: str) -> float:
        """实时价优先 (F4.3), 失败降级 daily_klines 昨收 + warning"""
        if self.data_layer is not None:
            try:
                prices = self.data_layer.get_realtime_prices([code])
                price = prices.get(code, 0)
                if price > 0:
                    return float(price)
            except Exception:
                pass
            logger.warning(f"[{code}] 实时价不可用, 降级 daily_klines 昨收")
        return self._get_prev_close(code)

    def _get_prev_close(self, code: str) -> float:
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT close FROM daily_klines WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,)
            ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

    def check(self, code: str, entry_plan: dict) -> dict | None:
        from common.utils import bj_now
        price = self._get_current_price(code)
        if price <= 0:
            logger.debug(f"[{code}] 价格无效: {price}")
            return None
        now = bj_now()
        current_time = now.split(" ")[1] if " " in now else "09:30"
        decision = self.entry_engine.check_entry(code, price, current_time)
        if decision is None:
            # R3/D1: 价格在买入区间内但被闸门拦截 → 透出 blocked 结构,
            # 供 monitor 推拦截蓝卡 (照推不静默, 色彩语义蓝=信息)。
            # 区间外的 None 保持原样返回 None (无卡可推)。
            block_reason = getattr(self.entry_engine, "_block_reason", None)
            if block_reason:
                low = float(entry_plan.get("entry_low") or 0)
                high = float(entry_plan.get("entry_high") or 0)
                if low > 0 and low <= price <= high:
                    logger.info(f"[{code}] 触价但被拦截: {block_reason}")
                    out = {"price": price, "reason": block_reason,
                           "triggered": False, "blocked": True}
                    # D1 续卡: 结构化 gate 一并透出 (供 entry_blocks 落库)
                    info = getattr(self.entry_engine, "_block_info", None) or {}
                    out["gate"] = info.get("gate")
                    out["block_detail"] = info.get("detail")
                    return out
            return None
        logger.info(f"[{code}] 入场触发: {decision.reason}")
        return {"price": decision.price, "reason": decision.reason, "triggered": True}
