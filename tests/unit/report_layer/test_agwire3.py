# AG-WIRE-3 (统筹 2026-09-08) 单测 — 接线盒双轨 + sector 首试点开闸
# 病: 报告层 5 处 llm_call 旁路绕过 agent 层 (不消毒/不留痕/不进计量/无三新键)
# 修: _llm_helper 双轨 — task 标记 → multi_agent_review 辩论链 (轨 B), 失败回落
#     叙述柜台 (轨 A); 首试点 sector_analysis 晨审节
from unittest.mock import MagicMock, patch

from report_layer.sections._llm_helper import llm_call, _review_via_agent


class _Ctx:
    """最小 context 桩: agent_layer + llm_service 双门面"""

    def __init__(self, agent=None, llm=None):
        self.agent_layer = agent
        self.llm_service = llm
        self.date = "2026-09-08"


class TestTrackB:
    def test_track_b_passes_through_reasoning(self):
        """轨 B 通: agent 层辩论链返回 reasoning → 直接采信, 不再调叙述柜台"""
        agent = MagicMock()
        resp = MagicMock()
        resp.reasoning = "六专家与裁决官综合研判: 板块轮动健康。"
        agent.analyze.return_value = resp
        ctx = _Ctx(agent=agent, llm=None)  # 叙述柜台缺席 — 轨 B 必须独立成立
        out = llm_call(ctx, "研判板块", call_point="x",
                       task="multi_agent_review:sector")
        assert "板块" in out
        agent.analyze.assert_called_once()
        req = agent.analyze.call_args[0][0]
        assert req.task == "multi_agent_review:sector"
        assert req.depth == "deep"
        assert req.style == "researcher" or req.style == "professional_advisor"
        assert req.consumer == "report"

    def test_track_b_failure_falls_back_to_track_a(self):
        """轨 B 败 (agent.analyze 抛异常) → 回落轨 A 叙述柜台"""
        agent = MagicMock()
        agent.analyze.side_effect = RuntimeError("LLM 全灭")
        llm = MagicMock()
        llm.is_available.return_value = True
        llm.call.return_value = "柜台降级文本"
        ctx = _Ctx(agent=agent, llm=llm)
        out = llm_call(ctx, "研判", call_point="x",
                       task="multi_agent_review:sector")
        assert out == "柜台降级文本"
        llm.call.assert_called_once()

    def test_track_b_empty_reasoning_falls_back(self):
        """轨 B 返回空 reasoning → None → 回落轨 A (铁律 #12 不静默)"""
        agent = MagicMock()
        resp = MagicMock()
        resp.reasoning = ""
        agent.analyze.return_value = resp
        llm = MagicMock()
        llm.is_available.return_value = True
        llm.call.return_value = "柜台文本"
        ctx = _Ctx(agent=agent, llm=llm)
        out = llm_call(ctx, "x", call_point="x", task="multi_agent_review:sector")
        assert out == "柜台文本"

    def test_no_agent_layer_short_circuits_to_a(self):
        """context 无 agent_layer → 轨 B 短路, 轨 A 照常 (5 旁路点既有形态)"""
        llm = MagicMock()
        llm.is_available.return_value = True
        llm.call.return_value = "纯柜台"
        ctx = _Ctx(agent=None, llm=llm)
        out = llm_call(ctx, "x", call_point="x", task="multi_agent_review:sector")
        assert out == "纯柜台"
        llm.call.assert_called_once()

    def test_three_keys_reach_request(self):
        """三新键穿到 AgentRequest (AG-COMPOSE 规范)"""
        agent = MagicMock()
        resp = MagicMock()
        resp.reasoning = "研报风格研判。"
        agent.analyze.return_value = resp
        ctx = _Ctx(agent=agent)
        llm_call(ctx, "x", call_point="x", task="multi_agent_review:sector",
                 style="researcher", max_words=180, focus=["capital_flow"])
        req = agent.analyze.call_args[0][0]
        assert req.style == "researcher"
        assert req.max_words == 180
        assert req.focus == ["capital_flow"]


class TestExistingCallersUnchanged:
    def test_call_without_task_never_touches_agent(self):
        """既有 4 旁路点 (不带 task 实参) → agent 层零触碰 (零行为变化守卫)"""
        agent = MagicMock()
        llm = MagicMock()
        llm.is_available.return_value = True
        llm.call.return_value = "原轨"
        ctx = _Ctx(agent=agent, llm=llm)
        out = llm_call(ctx, "x", call_point="exit_review", temperature=0.4)
        assert out == "原轨"
        agent.analyze.assert_not_called()
        llm.call.assert_called_once()


class TestSectorPilotGate:
    def test_sector_section_requests_multi_agent_review(self):
        """晨审 sector 节开闸断言: 请求带 task 前缀 + 三新键"""
        import inspect
        from report_layer.sections import sector_analysis
        src = inspect.getsource(sector_analysis)
        assert 'task="multi_agent_review:sector"' in src
        assert 'style="researcher"' in src
        assert 'focus=["sector_momentum", "capital_flow"]' in src

    def test_token_bucket_prefix_dispatch(self):
        """token_bucket "multi_agent_review:" 前缀分派到独立桶
        (判例「值断言禁写死」: 测分派机制而非额度数字 — 09-25 全表重设)"""
        from agent_layer.llm.token_budget import TokenBudget, TASK_BUDGETS
        mgr = TokenBudget()
        assert mgr.get_budget("multi_agent_review:sector") == TASK_BUDGETS["multi_agent_review"]
        assert mgr.get_budget("multi_agent_review:sector") != TASK_BUDGETS["default"],             "须落独立桶而非 default"
