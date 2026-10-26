"""分钟级技术指标计算"""
from strategy_layer.intraday.models import MinuteBar


def calc_rsi(bars: list[MinuteBar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 50.0
    closes = [b.close for b in bars[-period-1:]]
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(bars: list[MinuteBar]) -> dict:
    if len(bars) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0}
    closes = [b.close for b in bars]
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = ema(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]
    return {"macd": macd_line[-1], "signal": signal_line[-1], "histogram": histogram}


def calc_ma(bars: list[MinuteBar], period: int = 5) -> float:
    if len(bars) < period:
        return bars[-1].close if bars else 0
    return sum(b.close for b in bars[-period:]) / period


def analyze_minute_data(bars: list[MinuteBar], code: str) -> dict:
    result = {"code": code, "bar_count": len(bars)}
    if len(bars) < 5:
        result["signal"] = "insufficient_data"
        return result
    result["rsi_14"] = round(calc_rsi(bars, 14), 2)
    result["macd"] = calc_macd(bars)
    result["ma_5"] = round(calc_ma(bars, 5), 4)
    result["ma_20"] = round(calc_ma(bars, 20), 4)
    result["ma_60"] = round(calc_ma(bars, 60), 4)
    last = bars[-1]
    result["price"] = last.close
    signals = []
    if result["rsi_14"] > 70:
        signals.append("overbought")
    elif result["rsi_14"] < 30:
        signals.append("oversold")
    if result["macd"]["histogram"] > 0 and result["macd"]["macd"] > result["macd"]["signal"]:
        signals.append("macd_bullish")
    elif result["macd"]["histogram"] < 0 and result["macd"]["macd"] < result["macd"]["signal"]:
        signals.append("macd_bearish")
    if result["ma_5"] > result["ma_20"]:
        signals.append("short_above_long")
    else:
        signals.append("short_below_long")
    result["signals"] = signals
    return result
