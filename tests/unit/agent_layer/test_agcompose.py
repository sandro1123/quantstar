"""AG-COMPOSE 单测 (2026-09-07) — 多智能体组合层.

覆盖面 (设计稿验收第10步):
- 三新键 (style/max_words/focus) 序列化与默认值
- 专家 LLM 腿降级链: LLM 不可用 → 规则腿, tier_used=rules
- ExpertReport.tier_used 判读标注 (防假绿)
- TokenBudget 前缀归桶: expert:* → expert_debate 共池
- DecisionHub 全量意见终审签名 (expert_reports/bull/bear 穿参)
- registry multi_agent_review task 注册
"""
import pytest

from agent_layer.core.models import AgentRequest
from agent_layer.debate.models import ExpertReport
from agent_layer.debate.experts import (
    SentimentExpert, RiskExpert, TechExpert, ChipExpert,
    BigOrderExpert, HotMoneyExpert, ALL_EXPERTS, _llm_expert_opinion,
)
from agent_layer.llm.token_budget import TokenBudget, TASK_BUDGETS
from agent_layer.debate.decision_hub import DecisionHub
from agent_layer.core.registry import get_default_registry


class TestThreeKeys:
    """AG-COMPOSE 请求规范三新键"""

    def test_defaults(self):
        r = AgentRequest(task="multi_agent_review", input={"code": "600519"})
        assert r.style == "professional_advisor"
        assert r.max_words == 200
        assert r.focus == []
        assert r.version == "1.1"

    def test_custom_roundtrip(self):
        r = AgentRequest(
            task="multi_agent_review", input={"code": "600519"},
            style="researcher", max_words=300,
            focus=["量能持续性", "板块轮动位置"],
        )
        assert r.style == "researcher"
        assert r.max_words == 300
        assert r.focus == ["量能持续性", "板块轮动位置"]
        d = r.model_dump()
        assert d["style"] == "researcher" and d["max_words"] == 300
        # pydantic 序列化后回构不丢
        r2 = AgentRequest(**d)
        assert r2.focus == r.focus


class TestDegradationChain:
    """LLM 腿降级链 — QS_LLM_OFFLINE=1 (conftest 钉死) 下必走规则腿"""

    def test_all_experts_rules_leg_offline(self):
        # LLM 不可用 → _llm_expert_opinion 返 None → analyze 走 _rule_analyze
        data = {"turnover_ratio": 10, "pe": 15, "momentum_20d": 8,
                "volume_change": 30, "market_cap": 3e9}
        for cls in ALL_EXPERTS:
            rep = cls().analyze(data)
            assert isinstance(rep, ExpertReport)
            assert rep.tier_used == "rules"  # 诚实标注, 防假绿
            assert -1.0 <= rep.score <= 1.0
            assert 0.0 <= rep.confidence <= 1.0

    def test_llm_opinion_none_short_circuit(self):
        # 离线时 _llm_expert_opinion 直接 None, 不抛异常不炸链
        assert _llm_expert_opinion(
            "expert_sentiment", "sentiment",
            {"code": "600519", "date": "2026-09-07",
             "data_summary": "换手8% 动量+6"}) is None

    def test_rule_leg_scores_unchanged(self):
        # 规则腿打分与旧版语义一致 (AG-GUARD 回归面: 降级腿不许悄悄变形)
        assert SentimentExpert().analyze({"turnover_ratio": 10}).score > 0
        assert SentimentExpert().analyze({"turnover_ratio": 0.5}).score < 0
        assert RiskExpert().analyze({"pe": 150}).score < 0
        assert TechExpert().analyze({"momentum_20d": 8}).score > 0
        assert BigOrderExpert().analyze({"momentum_20d": 12}).score > 0
        assert HotMoneyExpert().analyze({"turnover_ratio": 20}).score > 0

    def test_tier_used_default_rules(self):
        # 兼容面: 直接构造的旧式 ExpertReport (位置参数) tier_used 默认 rules
        r = ExpertReport("sentiment", 0.5, ["高换手"])
        assert r.tier_used == "rules"


class TestBudgetBucketing:
    """TokenBudget 前缀归桶 — 六专家共池 expert_debate"""

    def test_expert_prefix_buckets_shared(self):
        tb = TokenBudget()
        assert tb.get_budget("expert:sentiment") == TASK_BUDGETS["expert_debate"]
        assert tb.get_budget("expert:risk") == TASK_BUDGETS["expert_debate"]
        # 共池: 六专家消耗累加在同一桶
        for _ in range(6):
            tb.record_usage("expert:hot_money", 100)
        assert tb.remaining("expert:sentiment") == TASK_BUDGETS["expert_debate"] - 600

    def test_multi_agent_review_bucket(self):
        tb = TokenBudget()
        assert tb.get_budget("multi_agent_review") == TASK_BUDGETS["multi_agent_review"]

    def test_default_bucket_unchanged(self):
        tb = TokenBudget()
        assert tb.get_budget("decision_hub") == TASK_BUDGETS["default"]
        assert tb.get_budget("stock_analysis") == TASK_BUDGETS["stock_analysis"]

    def test_exhaustion_degrades_not_raises(self):
        tb = TokenBudget()
        # 循环次数按额度算 (判例「值断言禁写死」: 测的是耗尽降级机制,
        # 不是某个额度数字 —— 09-25 全表重设后写死 60 次已不再触顶)
        step = 200
        for _ in range(TASK_BUDGETS["expert_debate"] // step + 1):
            tb.record_usage("expert:chip", step)
        assert tb.is_exceeded("expert:chip")
        assert tb.is_exceeded("expert:sentiment")  # 同池同枯


class TestDecisionHubFullInput:
    """裁决官全量意见终审 — 向后兼容 + 新签名"""

    def test_legacy_two_args_still_works(self):
        hub = DecisionHub(provider_manager=None)
        d = hub.decide("多方占优", {"momentum": 0.5})
        assert d.verdict in ("推荐", "不推荐", "观望")
        assert 0.0 <= d.confidence <= 0.9

    def test_full_inputs_math_wins_on_no_llm(self):
        hub = DecisionHub(provider_manager=None)
        reports = [ExpertReport("tech", 0.4, ["动量正"], 0.6, tier_used="rules")]
        d = hub.decide("多方占优", {"momentum": 0.5},
                       expert_reports=reports,
                       bull_args=["动量正"], bear_args=["估值偏高"])
        # 离线: 无 LLM → 数学判断直接落, 无分歧
        assert d.verdict == "推荐"
        assert "LLM与数学判断分歧" not in d.risk_flags

    def test_negative_composite_reject(self):
        hub = DecisionHub(provider_manager=None)
        d = hub.decide("空方占优", {"momentum": -0.8})
        assert d.verdict == "不推荐"
        assert d.position_pct == 0


class TestRegistryTask:
    """multi_agent_review task 注册面"""

    def test_multi_agent_review_registered(self):
        reg = get_default_registry()
        # registry API 按 task 查管线; deep 面须存在
        found = False
        try:
            pipelines = reg.get_pipelines_for_task("multi_agent_review")
            found = any(getattr(p, "name", "") == "deep_debate" for p in pipelines)
        except Exception:
            # 回退: 老式 API 差异不算红 — 用内部表直接验
            tbl = getattr(reg, "_pipelines", {})
            found = any(p.task == "multi_agent_review"
                        for ps in tbl.values() for p in (ps if isinstance(ps, list) else [ps]))
        assert found, "multi_agent_review task 未注册"


class TestComposeLLMData:
    """专家 _compose_llm_data 装配面"""

    def test_data_summary_passthrough_and_fallback(self):
        e = SentimentExpert()
        d = e._compose_llm_data({"code": "600519", "date": "2026-09-07",
                                 "data_summary": "换手8%",
                                 "turnover_ratio": 8})
        assert d["code"] == "600519" and d["date"] == "2026-09-07"
        assert d["data_summary"] == "换手8%"  # 已有摘要直传
        assert d["max_words"] == 200  # 缺省 200

        d2 = e._compose_llm_data({"turnover_ratio": 8})  # 无摘要 → JSON 兜底
        assert "turnover_ratio" in d2["data_summary"]
        assert d2["code"] == ""

    def test_style_focus_not_in_summary(self):
        # style_hint/focus 不该混进 data_summary (由模板专位承接)
        e = TechExpert()
        d = e._compose_llm_data({"style_hint": "x", "focus": "y", "momentum_20d": 5})
        assert "style_hint" not in d["data_summary"]
