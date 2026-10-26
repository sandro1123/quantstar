"""Machine-to-human reason translation layer (G-E3)"""

TRANSLATION_MAP = {
    "硬止损": "价格触及止损线，系统自动触发卖出。这是保护本金的第一道防线。",
    "止盈": "价格触及止盈目标，系统自动触发卖出。恭喜，收益已锁定！",
    "ATR止损": "基于波动率（ATR）的止损被触发。当前价格波动已超出正常范围，系统自动保护。",
    "移动止损": "价格从高点回撤超过设定比例，触发移动止盈。系统在保护已获利润的同时给了股价足够的波动空间。",
    "均线空头排列": "短期均线跌破中长期均线，形成死叉，技术面转弱。",
    "连续3日下跌": "连续3个交易日下跌，短期趋势走弱。",
    "RSI超买": "相对强弱指标（RSI）进入超买区，短期回调风险增大。",
    "超时": "持仓超过30天，按规则自动退出。长时间持有但未达到预期收益，说明选股逻辑可能需要调整。",
    "评分衰减": "策略评分持续下降，说明个股的基本面或技术面发生了不利变化。",
    "触价": "盘中价格触及预设的买入区间，系统自动执行买入。",
    "尾盘入场": "收盘前价格仍在买入区间内，系统在T+1风险最低的时段执行买入。",
    "追加买入": "评分连续3日上升且已有浮盈，系统判断可以加仓。",
    "减仓": "评分下降或风险增加，系统降低仓位。",
    "调仓换股": "发现同行业更优标的，系统进行换股操作。",
    "价格超过5日均价15%": "当前价格已超过5日均价15%，可能存在追高风险，系统自动跳过。",
    "价格超过开盘价5%": "当日涨幅已超过开盘价5%，追高性价比不高，系统自动跳过。",
    "5日涨幅超过15%": "近5日累计涨幅已超过15%，短期涨幅较大，系统自动跳过。",
    "RSI > 70": "RSI指标超过70，处于超买区域，系统自动跳过。",
}

SUB_REASON_MAP = {
    "layer_1_stop_loss": "硬止损-8%触达",
    "layer_1_take_profit": "止盈+25%触达",
    "layer_2_atr": "ATR波动率止损",
    "layer_3_trailing": "移动止盈-5%回撤",
    "layer_4_ma_death_cross": "均线死叉",
    "layer_4_3day_drop": "连续3日下跌",
    "layer_4_rsi_overbought": "RSI超买",
    "layer_5_timeout": "30天超时退出",
    "layer_6_score_decay": "评分衰减30%",
}


def translate_reason(machine_reason: str) -> str:
    """Translate machine reason to human-readable Chinese explanation"""
    for key, human in TRANSLATION_MAP.items():
        if key in machine_reason:
            return human
    return machine_reason


def get_sub_reason(layer: int, detail: str) -> str:
    """Get sub-reason classification for exit analysis (G-E2)"""
    key_map = {
        (1, "stop"): "layer_1_stop_loss",
        (1, "take"): "layer_1_take_profit",
        (2,): "layer_2_atr",
        (3,): "layer_3_trailing",
        (4, "ma"): "layer_4_ma_death_cross",
        (4, "3day"): "layer_4_3day_drop",
        (4, "rsi"): "layer_4_rsi_overbought",
        (5,): "layer_5_timeout",
        (6,): "layer_6_score_decay",
    }
    for key, sub_reason in key_map.items():
        if layer == key[0]:
            if len(key) == 1 or (len(key) > 1 and key[1] in detail.lower()):
                return sub_reason
    return f"layer_{layer}_unknown"


def format_value_saved_message(exit_decision) -> str | None:
    """Format value framing message (G-E21)"""
    if exit_decision.reason_layer == 3:
        return "移动止盈帮你锁定了利润"
    if exit_decision.reason_layer in (1, 2):
        return "止损帮你避免了更大的损失"
    return None
