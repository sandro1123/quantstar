from execution_layer.core.reason_translator import translate_reason, get_sub_reason, format_value_saved_message


def test_translate_hard_stop():
    result = translate_reason("硬止损: 14.0 <= 15.0")
    assert "价格触及止损线" in result


def test_translate_take_profit():
    result = translate_reason("止盈: 25.0 >= 20.0")
    assert "止盈目标" in result


def test_translate_atr_stop():
    result = translate_reason("ATR止损: 18.0 <= ATR价 19.00")
    assert "波动率" in result


def test_translate_trailing_stop():
    result = translate_reason("移动止损: 回撤8.0% > 5%")
    assert "价格从高点回撤" in result


def test_translate_ma_death_cross():
    result = translate_reason("均线空头排列")
    assert "短期均线跌破中长期均线" in result


def test_translate_ma_bullish():
    result = translate_reason("均线多头排列确认")
    assert result == "均线多头排列确认"


def test_translate_3day_drop():
    result = translate_reason("连续3日下跌")
    assert "连续3个交易日下跌" in result


def test_translate_rsi_overbought():
    result = translate_reason("RSI超买: 80.0 > 75")
    assert "进入超买区" in result


def test_translate_timeout():
    result = translate_reason("超时: 持有35天 >= 30天上限")
    assert "30天" in result


def test_translate_score_decay():
    result = translate_reason("评分衰减: 从80降至40 (-50%)")
    assert "评分持续下降" in result


def test_translate_spike_defense():
    result = translate_reason("价格超过5日均价15%")
    assert "追高风险" in result


def test_translate_unknown_reason():
    result = translate_reason("一些未知原因")
    assert result == "一些未知原因"


def test_get_sub_reason_stop_loss():
    assert get_sub_reason(1, "stop") == "layer_1_stop_loss"


def test_get_sub_reason_take_profit():
    assert get_sub_reason(1, "take") == "layer_1_take_profit"


def test_get_sub_reason_atr():
    assert get_sub_reason(2, "") == "layer_2_atr"


def test_get_sub_reason_trailing():
    assert get_sub_reason(3, "") == "layer_3_trailing"


def test_get_sub_reason_ma():
    assert get_sub_reason(4, "ma") == "layer_4_ma_death_cross"


def test_get_sub_reason_3day():
    assert get_sub_reason(4, "3day") == "layer_4_3day_drop"


def test_get_sub_reason_rsi():
    assert get_sub_reason(4, "rsi") == "layer_4_rsi_overbought"


def test_get_sub_reason_timeout():
    assert get_sub_reason(5, "") == "layer_5_timeout"


def test_get_sub_reason_score_decay():
    assert get_sub_reason(6, "") == "layer_6_score_decay"


def test_get_sub_reason_unknown():
    assert get_sub_reason(7, "") == "layer_7_unknown"


def test_format_value_saved_layer_1():
    class MockDecision:
        reason_layer = 1
    result = format_value_saved_message(MockDecision())
    assert result == "止损帮你避免了更大的损失"


def test_format_value_saved_layer_2():
    class MockDecision:
        reason_layer = 2
    result = format_value_saved_message(MockDecision())
    assert result == "止损帮你避免了更大的损失"


def test_format_value_saved_layer_3():
    class MockDecision:
        reason_layer = 3
    result = format_value_saved_message(MockDecision())
    assert result == "移动止盈帮你锁定了利润"


def test_format_value_saved_layer_4():
    class MockDecision:
        reason_layer = 4
    result = format_value_saved_message(MockDecision())
    assert result is None


def test_format_value_saved_layer_5():
    class MockDecision:
        reason_layer = 5
    result = format_value_saved_message(MockDecision())
    assert result is None