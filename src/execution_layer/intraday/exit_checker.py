"""盘中出场检查 — 使用 daily_klines 最新收盘价（避免 snapshot_intraday N+1 超时）"""
from common.database import Database
from common.logging import get_logger

logger = get_logger("execution.intraday.exit")


class IntradayExitChecker:
    def __init__(self, db: Database, stock_pool, exit_engine, data_layer=None):
        self.db = db
        self.stock_pool = stock_pool
        self.exit_engine = exit_engine
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

    def check(self, code: str, position: dict) -> dict | None:
        price = self._get_current_price(code)
        if price <= 0:
            return None
        # EX-05: 退出规则单一源 = exit_engine 6 层。原硬编码 price<=stop_loss 与
        # price>=entry_price*1.2 (20% 硬编码) 短路绕过引擎 (也绕过 T+1 保护),
        # 且止盈无视持仓自身 take_profit 参数, 已并入引擎 layer 1 (消费持仓自身
        # stop_loss/take_profit 参数); checker 只保留 取价 + 引擎调用 + 触发标记。
        if not self.exit_engine:
            return None
        try:
            kline_data = None
            if self.data_layer:
                try:
                    from common.utils import bj_date
                    hist = self.data_layer.get_history(code, bj_date(), 20)
                    if hist and hasattr(hist, 'klines'):
                        kline_data = hist.klines
                except Exception:
                    pass
            result = self.exit_engine.check_exits(code, price, kline_data)
            if result:
                return {"price": price, "reason": result.reason, "triggered": True, "action": "sell", "exit_low": result.exit_low}
        except Exception as e:
            logger.warning(f"[{code}] exit engine check failed: {e}")
        return None
