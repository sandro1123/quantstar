# -*- coding: utf-8 -*-
"""AG-DEBATE-V2 第一段 — RED 先行 (:memory:, 无网络, LLM 缺席走规则腿)。

派单 sm-64d3f480 三件:
  ① 2 轮制编排: 第 1 轮各专家独立 → 第 2 轮互看对方 key_points 修订
  ② ExpertReport.invalidation_conditions (失效条件, 对齐执行层条件单)
  ④ 干跑三重证: 编排可跑 / 配置在册 / 消费面能读
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from agent_layer.debate.models import ExpertReport  # noqa: E402
from agent_layer.debate import rounds_orchestrator as ro  # noqa: E402 ← RED: 模块不存在


# ---------------------------------------------------------------------------
# ② invalidation_conditions 字段
# ---------------------------------------------------------------------------

def test_expert_report_has_invalidation_conditions_default():
    """ExpertReport 新增失效条件字段, 缺省空 list (向后兼容)。"""
    r = ExpertReport(expert_name="t", score=0.5)
    assert r.invalidation_conditions == []


def test_invalidation_conditions_from_llm_opinion():
    """LLM 腿 opinion dict 带 invalidation_conditions 时透传进报告。"""
    r = ExpertReport(expert_name="t", score=0.5,
                     invalidation_conditions=["跌破 10 日线 -5%", "量能萎缩至 1/2"])
    assert len(r.invalidation_conditions) == 2


def test_round2_data_includes_round1_summary():
    """① 第 2 轮喂料: expert_data 含第 1 轮全专家摘要 (peer_review 字段)。"""
    reports = [
        ExpertReport(expert_name="sentiment", score=0.4, key_points=["换手率 6%"]),
        ExpertReport(expert_name="risk", score=-0.2, key_points=["高波动"]),
    ]
    data = ro.compose_round2_data(
        base_data={"code": "000001", "data_summary": "PE 12", "date": "2026-09-11"},
        round1_reports=reports)
    assert "peer_review" in data
    blob = data["peer_review"]
    assert "sentiment" in blob and "risk" in blob
    assert "换手率 6%" in blob           # 第 1 轮 key_points 可见
    assert data["data_summary"].startswith("PE 12")  # 原摘要保留


def test_round_config_key_in_defaults():
    """③ 配置在册: config/defaults/agent.yaml 出厂 debate_rounds=2。"""
    import yaml
    cfg_path = _root / "config" / "defaults" / "agent.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    assert cfg["agent"].get("debate_rounds") == 2


def test_get_rounds_from_config():
    """③ 消费面能读: rounds_config() 从 config dict 读, 缺省 2, clamp(1,2)。"""
    assert ro.rounds_config({}) == 2
    assert ro.rounds_config({"debate_rounds": 1}) == 1
    assert ro.rounds_config({"debate_rounds": 2}) == 2
    assert ro.rounds_config({"debate_rounds": 5}) == 2   # clamp 上界
    assert ro.rounds_config({"debate_rounds": 0}) == 2   # 非法值回落出厂


# ---------------------------------------------------------------------------
# ① 2 轮编排 + ④ 干跑三重证
# ---------------------------------------------------------------------------

def test_two_round_orchestration_dry_run():
    """④ 干跑: 假库上 2 轮辩论链全程组装成功 (无 LLM — 规则腿)。

    断言: 第 2 轮真发生 (专家被调两轮 / 第 2 轮数据含第 1 轮输出)。
    """
    calls = []

    class _ProbeExpert:
        name = "probe"
        def set_memory_db(self, db): pass
        def analyze(self, data):
            calls.append(dict(data))
            kp = [f"r{len(calls)} 观点"]  # 每轮不同 → 可断言轮次
            return ExpertReport(
                expert_name=self.name,
                score=0.3 if len(calls) == 1 else 0.5,
                key_points=kp, confidence=0.5, tier_used="rules")

    monkey = ro if hasattr(ro, "run_debate") else None
    out = ro.run_debate(
        base_data={"code": "000001", "date": "2026-09-11",
                   "data_summary": "PE 12, ROE 8", "factor_scores": {"roe": 0.7}},
        expert_classes=[_ProbeExpert],
        config={"debate_rounds": 2},
        provider_manager=None,   # 无 LLM — 全规则腿
    )
    assert len(calls) == 2                     # 2 轮各调一次
    assert "peer_review" in calls[1]           # 第 2 轮喂料含第 1 轮摘要
    assert "r1 观点" in calls[1]["peer_review"]
    # 终审输入含两轮报告
    assert len(out["round1_reports"]) == 1
    assert len(out["round2_reports"]) == 1
    assert out["rounds_run"] == 2


def test_single_round_when_config_1():
    """rounds=1 时只跑一轮 (config 可调真生效)。"""
    calls = []

    class _ProbeExpert:
        name = "probe"
        def set_memory_db(self, db): pass
        def analyze(self, data):
            calls.append(1)
            return ExpertReport(expert_name=self.name, score=0.2,
                                key_points=["k"], confidence=0.5)

    out = ro.run_debate(
        base_data={"code": "X", "date": "d", "data_summary": "s",
                   "factor_scores": {}},
        expert_classes=[_ProbeExpert],
        config={"debate_rounds": 1},
        provider_manager=None,
    )
    assert len(calls) == 1
    assert out["rounds_run"] == 1
    assert out["round2_reports"] == []


def test_invalidation_reaches_decision_input():
    """② 失效条件进终审输入: round2 报告的 invalidation_conditions 可被裁决面读。"""
    class _CondExpert:
        name = "cond"
        def set_memory_db(self, db): pass
        def analyze(self, data):
            return ExpertReport(
                expert_name=self.name, score=0.4, key_points=["k"],
                confidence=0.6,
                invalidation_conditions=["跌破 5 日线", "量能萎缩 50%"])

    out = ro.run_debate(
        base_data={"code": "X", "date": "d", "data_summary": "s",
                   "factor_scores": {}},
        expert_classes=[_CondExpert],
        config={"debate_rounds": 2},
        provider_manager=None,
    )
    conds = out["round2_reports"][0].invalidation_conditions
    assert "跌破 5 日线" in conds and "量能萎缩 50%" in conds
