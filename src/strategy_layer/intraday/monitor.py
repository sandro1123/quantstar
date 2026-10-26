"""IntradayStrategyMonitor — 盘中实时监控与信号

ST-02 (2026-08-18): 监控核心改调 data_layer.get_realtime_prices(codes)
(现成健康 API: 任意码表、80 只/批、逐源降级, 决策账本 S-11)。
原实现调 snapshot_intraday(code, date) 签名错位 (实际签名 (timestamp=None)),
恒抛 TypeError 被装饰器吞掉 → scan 自上线以来恒空 (审计 S-04)。
volume_ratio=2.0 硬编码已删除 (S-21): 无真实量比数据源时 volume_ratio 恒为
None 并在信号中标注 volume_ratio_available=False — 宁可没有, 不可是假的。
盘中监控真重建排队 (前置: DL-22 竞价采集 + 真实分钟数据, 决策账本 S-05)。
"""
from datetime import datetime, time

from common.logging import get_logger

logger = get_logger("strategy.intraday")

SURGE_THRESHOLD_PCT = 5.0
PLUNGE_THRESHOLD_PCT = -5.0


class IntradayStrategyMonitor:
    MARKET_OPEN = time(9, 30)
    MARKET_CLOSE = time(15, 0)

    def __init__(self, factor_engine=None, data_layer=None):
        self.factor_engine = factor_engine
        self.data_layer = data_layer
        self._running = False

    def is_trading_hours(self, dt: datetime | None = None) -> bool:
        now = dt or datetime.now()
        t = now.time()
        return self.MARKET_OPEN <= t <= self.MARKET_CLOSE

    def scan(self, codes: list[str], date: str) -> list[dict]:
        if not self.is_trading_hours():
            logger.info("Outside trading hours, skipping intraday scan")
            return []
        signals = []
        # 批量取实时价 (get_realtime_prices 内部 80 只/批 + 逐源降级), 替代原逐股 snapshot_intraday
        prices: dict[str, float] = {}
        if self.data_layer and codes:
            try:
                prices = self.data_layer.get_realtime_prices(list(codes)) or {}
            except Exception as e:
                logger.warning(f"Realtime price fetch failed: {e}")
        for code in codes:
            try:
                signal = self._check_stock(code, date, prices.get(code))
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"Intraday scan failed for {code}: {e}")
        logger.info(f"Intraday scan: {len(signals)} signals from {len(codes)} stocks")
        return signals

    def _prev_close(self, code: str, date: str) -> float | None:
        """前一交易日收盘价 (计算涨跌幅基准); 无数据返回 None"""
        try:
            hist = self.data_layer.get_history(code, date, 5)
            klines = [k for k in (hist.klines or []) if k.date < date and k.close]
            return klines[-1].close if klines else None
        except Exception:
            return None

    def _check_stock(self, code: str, date: str, price: float | None = None) -> dict | None:
        if not self.data_layer:
            return None
        try:
            if price is None:
                prices = self.data_layer.get_realtime_prices([code]) or {}
                price = prices.get(code)
            if not price:
                return None
            prev_close = self._prev_close(code, date)
            if not prev_close:
                return None
            change = (price - prev_close) / prev_close * 100
            # ST-02: 无真实量比数据源 — volume_ratio 恒 None + 显式标注 (原 volume>0→2.0 是假数据, S-21)
            if change > SURGE_THRESHOLD_PCT:
                return {"code": code, "date": date, "type": "surge",
                        "change_pct": round(change, 2),
                        "volume_ratio": None, "volume_ratio_available": False}
            if change < PLUNGE_THRESHOLD_PCT:
                return {"code": code, "date": date, "type": "plunge",
                        "change_pct": round(change, 2),
                        "volume_ratio": None, "volume_ratio_available": False}
            if self.factor_engine:
                try:
                    self.factor_engine.build_context(code, date, 10)
                    factor_result = self.factor_engine.compute_all([code], date)
                    if code in factor_result:
                        scores = factor_result[code]
                        composite = sum(scores.values()) / len(scores) if scores else 0
                        if abs(composite) > 1.5:
                            return {"code": code, "date": date, "type": "factor_signal", "composite": composite}
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Error checking {code}: {e}")
        return None

    def analyze_minute(self, code: str, bars: list) -> dict | None:
        from strategy_layer.intraday.indicators import analyze_minute_data
        if not bars or len(bars) < 5:
            return None
        analysis = analyze_minute_data(bars, code)
        if "oversold" in analysis.get("signals", []) and "macd_bullish" in analysis.get("signals", []):
            return {"code": code, "type": "minute_buy_signal", "analysis": analysis}
        if "overbought" in analysis.get("signals", []) and "macd_bearish" in analysis.get("signals", []):
            return {"code": code, "type": "minute_sell_signal", "analysis": analysis}
        return None

    def start_monitoring(self, codes: list[str], date: str, interval_s: int = 60):
        self._running = True
        logger.info(f"Intraday monitoring started for {len(codes)} stocks on {date}")
        while self._running and self.is_trading_hours():
            signals = self.scan(codes, date)
            if signals:
                logger.info(f"Found {len(signals)} intraday signals")
            import time
            time.sleep(interval_s)

    def stop_monitoring(self):
        self._running = False
        logger.info("Intraday monitoring stopped")
