"""ST-REGIME 情绪周期判定核 — 五阶段纯函数面 (第一段, 统筹 sm-b9bb1ee3213b4206)。

侦察稿设计 (qs-st-regime-recon-20260912):
  - 主变量: 梯队规模 (ladder COUNT DISTINCT code, 227~252 实测域) —
    max_board 恒 10 顶格饱和, 弃用 (侦察件二定谳)。
  - zt/dt 斜率与极值 (market_sentiment 25 日实测域: zt 34~110, dt 0~198)。
  - 五阶段: ice 冰点 / recovery 复苏 / ferment 发酵 / climax 高潮 / ebb 退潮。
  - 无恒 neutral 病: 五阶段是温度计必有归属; 序列不足诚实 unknown (不猜)。
  - 纯函数: 同输入同输出 (可复算锚); 判定证据全留痕 (落库表草案面)。

REPLAY 30 日分布账在测试夹具真数据回放面跑 (>80% 单阶段即回炉)。
"""
from common.logging import get_logger

logger = get_logger("strategy.regime.emotion_cycle")

# 参数锁 (单点定义, config 键化面 — 用户裁前的施工默认; 攒 40 交易日分布账后呈裁)
EMOTION_CYCLE_PARAMS = {
    "ladder_ice_floor": 200,   # 梯队坍缩线 (实测 227~252, 冰点形态 <200)
    "zt_ice_ceiling": 30,      # zt 萎缩线 (实测 34~110, 冰点形态 <30)
    "dt_ebb_floor": 30,        # dt 陡升线 (实测 08-19 形态 119)
    "zt_climax_floor": 90,     # zt 峰值线 (实测 08-10 103/08-17 110 形态)
    "slope_window": 3,         # 斜率窗 (3 日)
    "ferment_slope_min": 15,   # 发酵判定: zt 3 日斜率 >= 15 且梯队连升
}


def _slope(values: list[float]) -> float:
    """简单斜率: 末值 - 首值 (窗内), 上行正/下行负。"""
    if len(values) < 2:
        return 0.0
    return float(values[-1]) - float(values[0])


def classify_emotion_cycle(series: list[dict], params: dict | None = None) -> dict:
    """情绪周期五阶段判定 — 纯函数。

    Args:
        series: 升序日序列, 每日 {"date", "zt", "dt", "ladder_size"}
        params: 判定参数 (None → EMOTION_CYCLE_PARAMS 代码缺省;
            config 键化面由调用方 load_cycle_params 组装后传入)
    Returns:
        {"stage": str, "evidence": dict}
        stage ∈ {ice, recovery, ferment, climax, ebb, unknown}
    """
    p = params if params is not None else dict(EMOTION_CYCLE_PARAMS)
    w = p["slope_window"]
    if len(series) < w:
        return {"stage": "unknown",
                "evidence": {"reason": "insufficient_data",
                             "n_days": len(series)}}

    zt = [d["zt"] for d in series]
    dt = [d["dt"] for d in series]
    ladder = [d["ladder_size"] for d in series]
    zt_w = zt[-w:]
    dt_w = dt[-w:]
    ladder_w = ladder[-w:]

    zt_last, dt_last = zt[-1], dt[-1]
    ladder_last = ladder[-1]
    zt_slope = _slope(zt_w)
    ladder_rising = all(ladder_w[i + 1] > ladder_w[i] for i in range(len(ladder_w) - 1))
    ladder_falling = all(ladder_w[i + 1] < ladder_w[i] for i in range(len(ladder_w) - 1))

    evidence = {
        "zt_last": zt_last, "dt_last": dt_last, "ladder_last": ladder_last,
        "zt_slope": zt_slope, "dt_slope": _slope(dt_w),
        "ladder_rising": ladder_rising, "ladder_falling": ladder_falling,
    }

    # ---- 五阶段判定 (顺序即优先级: 极值形态先判, 趋势形态后判) ----
    # 1. 冰点: 梯队坍缩 或 zt 萎缩 (市场连板基因大面积消失)
    if ladder_last < p["ladder_ice_floor"] or zt_last < p["zt_ice_ceiling"]:
        evidence["hit"] = "ice_extreme"
        return {"stage": "ice", "evidence": evidence}

    # 2. 退潮: dt 陡升破线 (跌停潮 = 情绪出清)
    if dt_last >= p["dt_ebb_floor"]:
        evidence["hit"] = "ebb_dt_spike"
        return {"stage": "ebb", "evidence": evidence}

    # 3. 高潮: zt 破峰值线 (涨停潮顶格)
    if zt_last >= p["zt_climax_floor"]:
        evidence["hit"] = "climax_zt_peak"
        return {"stage": "climax", "evidence": evidence}

    # 4. 发酵: zt 斜率上行达发酵强度 且 梯队连升 (情绪扩散确认)
    if zt_slope >= p["ferment_slope_min"] and ladder_rising:
        evidence["hit"] = "ferment_rising"
        return {"stage": "ferment", "evidence": evidence}

    # 5. 复苏: zt 斜率转正 (尚未达发酵强度) — 梯队不坍缩前提已过闸
    if zt_slope > 0:
        evidence["hit"] = "recovery_slope"
        return {"stage": "recovery", "evidence": evidence}

    # 6. 平淡/阴跌但未达冰/退潮阈值: 温度计中间带 — 归 recovery 的"温度保持"档
    # (无 neutral 兜底: 设计层规避恒 neutral 病 — 中间带是复苏维持, 非无判定)
    evidence["hit"] = "recovery_plateau"
    return {"stage": "recovery", "evidence": evidence}
