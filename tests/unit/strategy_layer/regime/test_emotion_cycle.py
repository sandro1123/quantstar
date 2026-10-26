"""ST-REGIME 情绪周期判定核 RED→GREEN 夹具 (第一段)。

REPLAY 原料实测值锚 (2026-09-12 快照, mode=ro):
  zt 域: 34~110, dt 域: 0~198; 梯队规模 227~252 (12 快照)。
设计要点 (侦察稿四件, 统筹 sm-b9bb1ee3213b4206 批准施工):
  - 五阶段: ice(冰点) / recovery(复苏) / ferment(发酵) / climax(高潮) / ebb(退潮)
  - 主变量: 梯队规模 (ladder COUNT DISTINCT) + zt/dt 斜率极值 — max_board 恒 10 饱和弃用
  - 无恒 neutral 病: 阶段是市场温度计 (必有归属), 不是旧 regime 三态的扩展
  - 纯函数面: 输入=近 N 日 sentiment 序列+ladder 序列, 输出=五阶段之一+判定证据
  - 参数锁: config 键化 (ST_REGIME_PARAMS 单点定义)
"""
import pytest

from strategy_layer.regime.emotion_cycle import (
    EMOTION_CYCLE_PARAMS,
    classify_emotion_cycle,
)


def _day(date, zt, dt, ladder=250):
    return {"date": date, "zt": zt, "dt": dt, "ladder_size": ladder}


def _series(*days):
    """升序日序列 (d1=最旧)。"""
    return list(days)


class TestParamsLocked:
    def test_params_single_point(self):
        # 参数锁: 单点定义 (config 键化面, 用户裁前的施工默认)
        p = EMOTION_CYCLE_PARAMS
        assert p["ladder_ice_floor"] == 200
        assert p["zt_ice_ceiling"] == 30
        assert p["dt_ebb_floor"] == 30
        assert p["zt_climax_floor"] == 90
        assert p["slope_window"] == 3

    def test_params_immutable_shape(self):
        assert set(EMOTION_CYCLE_PARAMS) == {
            "ladder_ice_floor", "zt_ice_ceiling", "dt_ebb_floor",
            "zt_climax_floor", "slope_window", "ferment_slope_min"}


class TestFiveStages:
    def test_ice_ladder_collapse(self):
        # 冰点: 梯队坍缩 (<200) 且 zt 萎缩 (<30)
        s = _series(_day("2026-09-01", 25, 40, 180), _day("2026-09-02", 22, 45, 175),
                    _day("2026-09-03", 20, 50, 170))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "ice"

    def test_ice_by_zt_collapse_only(self):
        # 梯队尚可但 zt 极端萎缩 (如 07-17 实测 zt=34 dt=198 形态邻域)
        s = _series(_day("2026-09-01", 28, 100, 240), _day("2026-09-02", 25, 120, 235),
                    _day("2026-09-03", 22, 150, 230))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "ice"

    def test_climax_zt_peak(self):
        # 高潮: zt 破 90 (08-17 实测 110 形态; 即使同时满足发酵斜率, 极值优先)
        s = _series(_day("2026-09-01", 70, 3, 240), _day("2026-09-02", 85, 2, 248),
                    _day("2026-09-03", 110, 1, 252))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "climax"
        assert r["evidence"]["hit"] == "climax_zt_peak"

    def test_climax_priority_over_ferment(self):
        # 判定顺序锁: 极值形态 (climax) 先于趋势形态 (ferment) —
        # zt 末值撞线且梯队连升时归 climax 非 ferment
        s = _series(_day("2026-09-01", 50, 5, 230), _day("2026-09-02", 70, 4, 240),
                    _day("2026-09-03", 95, 2, 252))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "climax"

    def test_ebb_dt_spike(self):
        # 退潮: dt 陡升破 30 (08-19 实测 119 形态)
        s = _series(_day("2026-09-01", 60, 5, 250), _day("2026-09-02", 45, 25, 245),
                    _day("2026-09-03", 38, 119, 235))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "ebb"

    def test_ferment_rising_slope(self):
        # 发酵: 梯队 3 日连升 + zt 上行斜率 (末值不撞 climax 线 90)
        s = _series(_day("2026-09-05", 50, 5, 230), _day("2026-09-06", 60, 4, 238),
                    _day("2026-09-07", 80, 2, 250))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "ferment"

    def test_recovery_zt_slope_turns_positive(self):
        # 复苏: zt 斜率转正但未达发酵强度 (斜率<15 或梯队未连升 — 本例斜率 18 但
        # 梯队平走未连升, 不满足 ferment 双条件)
        s = _series(_day("2026-09-01", 30, 20, 210), _day("2026-09-02", 38, 15, 214),
                    _day("2026-09-03", 48, 10, 214))
        r = classify_emotion_cycle(s)
        assert r["stage"] == "recovery"

    def test_evidence_dict_present(self):
        # 留痕: 判定证据 dict (斜率/极值/阈值命中面) — 供落库表
        s = _series(_day("2026-09-01", 70, 3, 240), _day("2026-09-02", 85, 2, 248),
                    _day("2026-09-03", 110, 1, 252))
        r = classify_emotion_cycle(s)
        assert "evidence" in r
        assert r["evidence"]["zt_last"] == 110
        assert r["evidence"]["ladder_last"] == 252


class TestPurity:
    def test_pure_function_no_side_effect(self):
        # 可复算锚: 同输入同输出 (纯函数, 无内部状态)
        s = _series(_day("2026-09-01", 40, 21, 252), _day("2026-09-02", 49, 7, 250),
                    _day("2026-09-03", 74, 0, 248))
        r1 = classify_emotion_cycle(s)
        r2 = classify_emotion_cycle(list(s))
        assert r1 == r2

    def test_insufficient_data(self):
        # 序列不足: 诚实降级 — 阶段 unknown + 证据留痕 (不猜)
        r = classify_emotion_cycle([_day("2026-09-01", 40, 2, 250)])
        assert r["stage"] == "unknown"
        assert r["evidence"]["reason"] == "insufficient_data"

    def test_empty_series(self):
        r = classify_emotion_cycle([])
        assert r["stage"] == "unknown"


class TestNoConstantNeutral:
    def test_stages_cover_observables_not_default(self):
        # 无恒 neutral 病 (设计层): 任何足长序列必落五阶段之一或 unknown —
        # 不存在"全不命中→neutral"的兜底路径
        samples = [
            [_day("2026-09-01", 40, 21, 250), _day("2026-09-02", 41, 20, 250),
             _day("2026-09-03", 42, 19, 250)],   # 平淡序列
            [_day("2026-09-01", 55, 8, 245), _day("2026-09-02", 58, 6, 247),
             _day("2026-09-03", 61, 4, 249)],   # 温和上行
        ]
        for s in samples:
            r = classify_emotion_cycle(s)
            assert r["stage"] in ("ice", "recovery", "ferment", "climax", "ebb"), \
                f"序列 {s[0]['date']}~{s[-1]['date']} 落到非法阶段 {r['stage']}"
