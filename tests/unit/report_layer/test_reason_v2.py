# -*- coding: utf-8 -*-
"""理由位降级序 v2 TDD (09-29 03:00 窗, 用户视角席三洞修)。

三洞: ① advisor_text=0/14 优先字段全空永远走不到第一档 ② rationale 是
对象非字符串 isinstance(val,str) 失败跳过落排名话术 ③ 辩论池≠推荐池。
降级序 (保铁闸「永不回落排名话术」): advisor_text → rationale 展开
(reasoning 人话/key_points 首条) → added_reason 非排名话术 → 诚实占位。
忠实性守卫: rationale.verdict「观望」不许渲成「买入」。
"""
import pytest


def _pick(row):
    from report_reason import _pick_reason
    return _pick_reason(row)


# 钉①: 第一档 advisor_text 非空即用
def test_tier1_advisor_text():
    row = {"advisor_text": "agent 看好：价值低估+质量稳"}
    assert _pick(row) == "agent 看好：价值低估+质量稳"


# 钉②: advisor_text 空 → rationale 展开 reasoning 人话 (三洞修②)
def test_tier2_rationale_reasoning():
    row = {
        "advisor_text": None,
        "recommendation_rationale": {
            "reasoning": "【观望】短线受融资盘推动具备资金动量，但聪明资金缺席",
            "key_points": '["公告硬信息：中报净利润稳健", "异动资金面"]',
        },
    }
    got = _pick(row)
    assert "观望" in got, f"rationale reasoning 人话应透出: {got}"
    assert "候选池第" not in got


# 钉③: reasoning 空 → key_points 首条 (JSON list)
def test_tier2_rationale_keypoints_first():
    row = {
        "advisor_text": None,
        "recommendation_rationale": {
            "reasoning": "",
            "key_points": '["公告硬信息：中报净利润稳健", "异动资金面"]',
        },
    }
    got = _pick(row)
    assert "公告硬信息" in got, f"key_points 首条应透出: {got}"


# 钉④: rationale 全空 → added_reason 非排名话术原样
def test_tier3_added_reason_non_rank():
    row = {
        "advisor_text": None,
        "recommendation_rationale": None,
        "added_reason": "基本面改善+技术面突破",
    }
    assert _pick(row) == "基本面改善+技术面突破"


# 钉⑤: 铁闸 — added_reason 排名话术永不渲染, 降诚实占位
def test_iron_gate_rank_phrase_never_render():
    row = {
        "advisor_text": None,
        "recommendation_rationale": None,
        "added_reason": "候选池第 1 位",
    }
    got = _pick(row)
    assert "候选池第" not in got, "排名话术永不回落"
    assert "生成中" in got, "降诚实占位"


# 钉⑥: 全空 → 诚实占位
def test_all_empty_honest_placeholder():
    row = {"advisor_text": None, "recommendation_rationale": None, "added_reason": None}
    got = _pick(row)
    assert "生成中" in got


# 钉⑦: 忠实性守卫 — verdict 观望 vote 理由不得渲成买入 (方向由调用方渲染,
# 此处锁理由文本不含反向祈使)
def test_fidelity_watch_not_buy():
    row = {
        "advisor_text": None,
        "recommendation_rationale": {
            "verdict": "观望",
            "reasoning": "【观望】杠杆集中易引发踩踏，建议耐心等待",
        },
    }
    got = _pick(row)
    assert "观望" in got
    assert "买入" not in got, "观望 vote 理由不得渲成买入"


# 钉⑧: rationale 是裸字符串 (旧格式兼容) — 直用 (第一档语义, 旧契约)
def test_rationale_string_compat():
    row = {
        "advisor_text": None,
        "recommendation_rationale": "旧格式字符串理由",
        "added_reason": "兜底理由",
    }
    got = _pick(row)
    # 裸字符串 _expand_rationale 直用 (旧契约: rationale 存在即用)
    assert got == "旧格式字符串理由"
