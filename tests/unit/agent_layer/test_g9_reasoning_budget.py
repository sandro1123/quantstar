# -*- coding: utf-8 -*-
"""G9 测试钉：推理模型 max_tokens 预算隔离。

事故：cbai/hy3 吐 2857 字思考 → 2000 tokens 耗尽 → content 0 字 →
      empty response → 主备双挂 → 熔断 → 晚报 120s 超时 → 没发出去。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))


def test_reasoning_provider_floor_lifts_requested():
    """钉1 (核心): 推理模型配了 reasoning_floor，调用方给的小预算被抬到 floor。"""
    from agent_layer.llm.client import _effective_max_tokens

    prov = {"name": "r10-hy3-backup", "model": "cbai/hy3", "reasoning_floor": 8000}
    assert _effective_max_tokens(prov, 2000) == 8000, (
        "推理模型给小预算应被抬到 reasoning_floor")


def test_non_reasoning_provider_unchanged():
    """钉2 (反向): 普通模型没配 floor，requested 原样返回（行为逐字节不变）。"""
    from agent_layer.llm.client import _effective_max_tokens

    prov = {"name": "r10-glm52", "model": "cbcn/glm-5.2"}
    assert _effective_max_tokens(prov, 2000) == 2000
    assert _effective_max_tokens(prov, 500) == 500


def test_requested_above_floor_not_lowered():
    """钉3 (边界): 调用方给的预算已高于 floor，不得被降低。"""
    from agent_layer.llm.client import _effective_max_tokens

    prov = {"name": "r10-hy3-primary", "model": "cbcn/hy3", "reasoning_floor": 6000}
    assert _effective_max_tokens(prov, 20000) == 20000


def test_malformed_floor_falls_back_safely():
    """钉4 (鲁棒): floor 是非数字/None 时不能崩，按无 floor 处理。"""
    from agent_layer.llm.client import _effective_max_tokens

    for bad in (None, "abc", "", 0, -5):
        prov = {"name": "x", "reasoning_floor": bad}
        assert _effective_max_tokens(prov, 2000) == 2000, f"floor={bad!r} 应回落原值"


def test_production_config_has_reasoning_floor_on_hy3():
    """钉5 (配置契约): 生产 llm.yaml 的 hy3 腿必须配 reasoning_floor。

    这条跨到配置文件 —— 只测函数不测配置，等于没守护。
    """
    import yaml
    p = os.path.join(ROOT, "config", "defaults", "llm.yaml")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    hy3 = [x for x in cfg["llm"]["providers"] if "hy3" in x.get("model", "")]
    assert hy3, "配置里应有 hy3 腿"
    for p_ in hy3:
        floor = p_.get("reasoning_floor")
        assert floor and int(floor) >= 6000, (
            f"{p_['name']} ({p_['model']}) 缺 reasoning_floor 或过低: {floor} "
            "—— 推理模型思考会吃完 token, content 会是空")
