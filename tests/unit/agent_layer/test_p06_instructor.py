"""P0-6: DecisionAgent instructor 结构化输出 — targeted 红绿

覆盖:
1. instructor 解析合法 JSON → parser=instructor, 字段正确
2. instructor 对坏 JSON 带错重试 → 2 次调用后成功
3. schema 拒绝 confidence>1 → 触发重试
4. instructor 不可用(返回 None) → 落 legacy 行解析 → parser=legacy
5. instructor 与 legacy 都失败 → rule_based 兜底
6. 传输仍走 pm.call (9router 链), instructor 不自己发 HTTP
"""
import json

import pytest

from agent_layer.agents import decision_agent as da_mod
from agent_layer.agents.decision_agent import DecisionAgent
from agent_layer.llm.structured_output import (
    DecisionVerdict,
    parse_decision_via_instructor,
)


class _FakeClient:
    last_used_provider = "fake-provider"


class FakePM:
    """按序返回 .call() 结果; 耗尽后重复最后一个。记录每次 prompt。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.client = _FakeClient()

    def call(self, prompt, system_prompt=None, temperature=0.2,
             max_tokens=500, call_point="x", timeout=None, cache_ttl=None):
        self.calls.append(prompt)
        if len(self.calls) <= len(self._responses):
            return self._responses[len(self.calls) - 1]
        return self._responses[-1]

    def has_available_llm(self, quality="low"):
        return True


GOOD = json.dumps({"verdict": "推荐", "confidence": 0.72,
                   "reasoning": "因子共振", "risk_flags": ["流动性"]},
                  ensure_ascii=False)
BAD = "Sure! {verdict: 推荐, confidence: 1.7} not json"


def _data():
    return {"code": "600519", "factor_scores": {"momentum": 0.8, "value": 0.6},
            "market_regime": "bull", "analysis_summary": ""}


def test_instructor_parses_valid_json():
    pm = FakePM([GOOD])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    assert result.verdict == "推荐"
    assert result.confidence == 0.72
    assert result.reasoning == "因子共振"
    assert result.risk_flags == ["流动性"]
    assert result.quality["parser"] == "instructor"
    assert len(pm.calls) == 1, "合法 JSON 一次即成, 不应重试"


def test_instructor_retries_bad_then_good():
    pm = FakePM([BAD, GOOD])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    assert result.verdict == "推荐"
    assert result.quality["parser"] == "instructor"
    assert len(pm.calls) == 2, "坏 JSON 应触发一次带错重试"


def test_schema_rejects_confidence_out_of_range():
    over = json.dumps({"verdict": "推荐", "confidence": 1.7,
                       "reasoning": "x", "risk_flags": []})
    pm = FakePM([over, GOOD])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    # confidence=1.7 越界 → pydantic 拒绝 → 重试拿到 GOOD
    assert result.confidence == 0.72
    assert result.quality["parser"] == "instructor"
    assert len(pm.calls) == 2


def test_falls_to_legacy_when_instructor_unavailable(monkeypatch):
    # 模拟 instructor 未安装: parse_decision_via_instructor 返回 None
    monkeypatch.setattr(da_mod, "parse_decision_via_instructor",
                        lambda *a, **k: None)
    line_text = ("verdict: 观望\nconfidence: 0.5\n"
                 "reasoning: 信号不足\nrisk_flags: 量能,情绪")
    pm = FakePM([line_text])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    assert result.verdict == "观望"
    assert result.quality["parser"] == "legacy"
    assert result.risk_flags == ["量能", "情绪"]


def test_all_paths_fail_falls_to_rule_based(monkeypatch):
    monkeypatch.setattr(da_mod, "parse_decision_via_instructor",
                        lambda *a, **k: None)
    pm = FakePM(["完全不是结构化输出的自由文本"])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    # legacy 解析失败 → rule_based (composite=0.7 → 推荐)
    assert result.quality.get("parser") is None
    assert result.quality["tier_used"] == "rule_fallback"
    assert result.verdict == "推荐"


def test_no_llm_still_rule_based():
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({"code": "000001",
                            "factor_scores": {"momentum": 0.8, "value": 0.6}})
    assert result.verdict == "推荐"
    assert result.quality["tier_used"] == "rule_fallback"


def test_schema_direct_validation():
    v = DecisionVerdict(verdict="推荐", confidence=0.5,
                        reasoning="r", risk_flags=["a"])
    assert v.confidence == 0.5
    with pytest.raises(Exception):
        DecisionVerdict(verdict="推荐", confidence=2.0)


def test_parse_returns_none_on_exhaustion():
    # instructor 装好但模型持续坏 → 重试耗尽返回 None (交调用方兜底)
    pm = FakePM([BAD])
    out = parse_decision_via_instructor(
        pm, prompt="p", system_prompt="s", call_point="decision:test",
        max_retries=2)
    assert out is None


def test_pm_returns_none_short_circuits_no_retry():
    """pm.call 返 None (provider 链无响应) 必须短路, 不得空转重试 3 次。

    现状 bug: None 被适配器包成 content="" → instructor JSON 解析必败 →
    带错重试 max_retries 次, 每次都完整重走 9router 全链, 拖慢管道
    (build_evening_report 60s 超时相关)。修法: 适配器对 None 抛异常,
    instructor 立即放弃 (非校验异常不重试) → 只调 1 次。
    """
    pm = FakePM([None])
    out = parse_decision_via_instructor(
        pm, prompt="p", system_prompt="s", call_point="decision:test",
        max_retries=2)
    assert out is None
    assert len(pm.calls) == 1, "pm.call 返 None 应短路, 不应空转重试"


def test_agent_pm_none_falls_to_rule_based_minimal_calls():
    """agent 全链: pm 持续返 None → instructor 短路 + legacy 也拿 None
    → rule_based 兜底; 总调用 = 1(instructor) + 1(legacy) = 2, 而非 3+1。"""
    pm = FakePM([None])
    agent = DecisionAgent(provider_manager=pm)
    result = agent.analyze(_data())
    assert result.quality["tier_used"] == "rule_fallback"
    assert len(pm.calls) == 2, "instructor 短路后 legacy 兜底各一次, 不得空转"
