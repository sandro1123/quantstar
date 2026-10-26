"""分钟级技术指标 — 单元测试"""
import pytest
from data_layer._models import MinuteBar
from strategy_layer.intraday.indicators import calc_rsi, calc_macd, calc_ma, analyze_minute_data


def _make_bars(closes: list[float], code: str = "000001") -> list[MinuteBar]:
    return [MinuteBar(code=code, time=f"09:{i+30:02d}", close=c) for i, c in enumerate(closes)]


class TestCalcRSI:
    def test_insufficient_data(self):
        bars = _make_bars([10.0] * 5)
        assert calc_rsi(bars, 14) == 50.0

    def test_rsi_100_on_straight_up(self):
        bars = _make_bars([10.0 + i * 0.1 for i in range(20)])
        assert calc_rsi(bars, 14) == 100.0

    def test_rsi_0_on_straight_down(self):
        bars = _make_bars([10.0 - i * 0.1 for i in range(20)])
        assert calc_rsi(bars, 14) == 0.0

    def test_rsi_neutral(self):
        bars = _make_bars([10.0] * 5 + [10.0 + 0.5, 10.0 - 0.5, 10.0 + 0.5, 10.0 - 0.5] * 4)
        rsi = calc_rsi(bars, 14)
        assert 30 < rsi < 70


class TestCalcMACD:
    def test_insufficient_data(self):
        bars = _make_bars([10.0] * 10)
        result = calc_macd(bars)
        assert result["macd"] == 0
        assert result["signal"] == 0
        assert result["histogram"] == 0

    def test_macd_on_uptrend(self):
        bars = _make_bars([10.0 + i * 0.05 for i in range(60)])
        result = calc_macd(bars)
        assert result["macd"] > 0
        assert result["histogram"] != 0

    def test_macd_on_downtrend(self):
        bars = _make_bars([10.0 - i * 0.05 for i in range(60)])
        result = calc_macd(bars)
        assert result["macd"] < 0

    def test_macd_return_type(self):
        bars = _make_bars([10.0] * 30)
        result = calc_macd(bars)
        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result


class TestCalcMA:
    def test_insufficient_data(self):
        bars = _make_bars([10.0] * 3)
        assert calc_ma(bars, 5) == 10.0

    def test_empty_bars(self):
        assert calc_ma([], 5) == 0

    def test_ma_calculation(self):
        bars = _make_bars([1, 2, 3, 4, 5])
        assert calc_ma(bars, 5) == 3.0

    def test_ma_partial(self):
        bars = _make_bars([1, 2, 3])
        assert calc_ma(bars, 5) == 3.0


class TestAnalyzeMinuteData:
    def test_insufficient_data(self):
        bars = _make_bars([10.0] * 3)
        result = analyze_minute_data(bars, "000001")
        assert result["signal"] == "insufficient_data"

    def test_normal_analysis(self):
        bars = _make_bars([10.0 + i * 0.1 for i in range(60)])
        result = analyze_minute_data(bars, "000001")
        assert result["code"] == "000001"
        assert result["bar_count"] == 60
        assert "rsi_14" in result
        assert "macd" in result
        assert "ma_5" in result
        assert "ma_20" in result
        assert "ma_60" in result
        assert "signals" in result
        assert result["price"] == bars[-1].close

    def test_oversold_signal(self):
        closes = [10.0 - i * 0.3 for i in range(60)]
        bars = _make_bars(closes)
        result = analyze_minute_data(bars, "000001")
        assert "oversold" in result["signals"]

    def test_ma_crossover_signal(self):
        bars = _make_bars(list(range(5, 65)))
        result = analyze_minute_data(bars, "000001")
        assert "short_above_long" in result["signals"]