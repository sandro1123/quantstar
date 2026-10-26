"""EasyTDX 分钟级数据源 — TCP 直连通达信"""
from datetime import datetime, timedelta
from typing import Any

from data_layer._models import MinuteBar

BJT = timedelta(hours=8)


class EasyTDXSourceAdapter:
    """EasyTDX 数据源适配器 — 分钟K线/分时/逐笔"""

    @property
    def name(self) -> str:
        return "easy_tdx"

    @property
    def capabilities(self) -> set[str]:
        return {"minute_data", "moneyflow", "stock_list", "tick_data"}

    def is_available(self) -> bool:
        try:
            from easy_tdx import TdxClient
            client = TdxClient.from_best_host()
            client.connect()
            client.disconnect()
            return True
        except Exception:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type == "minute_data":
            return self._get_minute_data(params)
        elif data_type == "tick_data":
            return self._get_tick_data(params)
        return None

    def _get_minute_data(self, params: dict) -> list[MinuteBar]:
        """获取分钟K线（1/3/5/15/30/60分钟）"""
        code = params.get("code", "")
        period = params.get("period", 1)
        count = params.get("count", 240)
        try:
            from easy_tdx import TdxClient
            client = TdxClient.from_best_host()
            client.connect()
            market = 1 if code.startswith("6") else 0
            period_map = {1: 7, 3: 8, 5: 0, 15: 1, 30: 2, 60: 3}
            category = period_map.get(period, 7)
            bars = client.get_security_bars(market, code, category, 0, count)
            client.disconnect()
            if bars is None or (hasattr(bars, 'empty') and bars.empty):
                return []
            if hasattr(bars, 'to_dict'):
                records = bars.to_dict('records') if hasattr(bars, 'to_dict') else []
            elif isinstance(bars, list):
                records = bars
            else:
                return []
            results = []
            for bar in records:
                raw = bar if isinstance(bar, dict) else bar.__dict__
                y = raw.get("year", 0) or 0
                m = raw.get("month", 0) or 0
                d = raw.get("day", 0) or 0
                h = raw.get("hour", 0) or 0
                minute = raw.get("minute", 0) or 0
                if y and m and d:
                    t = f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{minute:02d}:00"
                else:
                    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                results.append(MinuteBar(
                    code=code,
                    time=t,
                    open=float(raw.get("open", 0) or 0),
                    high=float(raw.get("high", 0) or 0),
                    low=float(raw.get("low", 0) or 0),
                    close=float(raw.get("close", 0) or 0),
                    volume=float(raw.get("vol", 0) or 0),
                ))
            return results
        except Exception as e:
            logger = __import__("logging").getLogger("data.easy_tdx")
            logger.warning(f"EasyTDX minute data failed for {code}: {e}")
            return []

    def _get_tick_data(self, params: dict) -> list[dict]:
        """获取当日逐笔成交数据"""
        code = params.get("code", "")
        start = params.get("start", 0)
        count = params.get("count", 800)
        try:
            from easy_tdx import TdxClient
            client = TdxClient.from_best_host()
            client.connect()
            market = 1 if code.startswith("6") else 0
            ticks = client.get_transaction_data(market, code, start, count)
            client.disconnect()
            if not ticks:
                return []
            return [dict(t) if isinstance(t, dict) else t.__dict__ for t in ticks]
        except Exception as e:
            logger = __import__("logging").getLogger("data.easy_tdx")
            logger.warning(f"EasyTDX tick data failed for {code}: {e}")
            return []

    def get_minute_kline(self, code: str, period: int = 1, count: int = 240) -> list[MinuteBar]:
        """公开方法：获取分钟K线"""
        return self._get_minute_data({"code": code, "period": period, "count": count})

    def get_intraday_timeseries(self, code: str) -> list[MinuteBar]:
        """获取今日分时时间序列（240点，无需参数）"""
        return self._get_minute_data({"code": code, "period": 1, "count": 240})

    def get_tick_data(self, code: str, start: int = 0, count: int = 800) -> list[dict]:
        """获取逐笔成交"""
        return self._get_tick_data({"code": code, "start": start, "count": count})
