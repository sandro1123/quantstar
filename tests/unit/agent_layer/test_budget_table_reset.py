# -*- coding: utf-8 -*-
"""额度全表重设 + deep_top_n 5→10 TDD（用户裁定 09-25，统筹算账 sm-3a1712eb）

■ 定位裁定（本卡的根）：budget 的真作用是**防失控**（异常循环烧穿），不是省钱。
  原值按「省钱」设计 → 正常用量就被卡死（09-24 实测 expert_debate 6000 在
  16:39 耗尽，当日辩论腿全断 132 次）。新原则 = 正常用量 × 10 倍余量。

■ 成本实测账（统筹核）：辩论腿走 cbcn/glm-5.2（倍率 0.47，**prompt token 不计费
  只算补全**）→ 全覆盖 10只×6专家=60次/天 ≈ 3万补全token ≈ 14 积分/天；
  用户池 9365 存量 + 每日签到回血 300 → 回血是消耗的 21 倍。
  用户亲定（09-03 在册，09-25 重申）：「积分不是约束，质量优先」。

■ deep_top_n 5→10 是本卡正题：用户亲自发现「晚报选股理由全是生成中」——
  辩论只覆盖 deep_top_n 只，其余票的理由永远是占位。扩到 10 全覆盖推荐面。

判例「有注释≠有约束」正面用法：注释承载成本决策依据+批准锚，测试钉住
「依据注释与值同在」，防后人凭感觉调。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_layer.llm.token_budget import TASK_BUDGETS

# 用户批准的全表（统筹 09-25 算账，改这张表须重走批准）
APPROVED_TABLE = {
    "morning_report": 50000,
    "evening_report": 50000,
    "urgent_analysis": 20000,
    "stock_analysis": 10000,
    "entry_check": 5000,
    "exit_review": 5000,
    "quick_check": 2000,
    "debate": 50000,
    "committee": 50000,
    "expert_debate": 300000,
    "multi_agent_review": 100000,
    "default": 5000,
}

# 重设前的旧值（病案锚：这些值让正常用量被卡死）
OLD_TABLE = {
    "morning_report": 2000, "evening_report": 2000, "urgent_analysis": 1000,
    "stock_analysis": 500, "entry_check": 300, "exit_review": 300,
    "quick_check": 100, "debate": 2000, "committee": 2000,
    "expert_debate": 6000, "multi_agent_review": 3000, "default": 500,
}


class TestBudgetTableReset:
    def test_full_table_matches_approved(self):
        """全表 12 项逐项对拍用户批准值（防漏改/防多改）"""
        assert TASK_BUDGETS == APPROVED_TABLE, (
            f"额度表与批准值不符\n多出: {set(TASK_BUDGETS) - set(APPROVED_TABLE)}\n"
            f"缺失: {set(APPROVED_TABLE) - set(TASK_BUDGETS)}")

    @pytest.mark.parametrize("key", sorted(APPROVED_TABLE))
    def test_every_entry_enlarged(self, key):
        """全表每一项都必须比旧值大（本卡是放大不是调整，反向锚）"""
        assert TASK_BUDGETS[key] > OLD_TABLE[key], f"{key} 未放大"

    def test_expert_debate_is_300k(self):
        """主角项：10只×6专家×~500补全token×10倍余量 = 30万"""
        assert TASK_BUDGETS["expert_debate"] == 300000

    def test_decision_evidence_annotated(self):
        """判例正面用法：成本决策依据必须与值同在（算账式/实测锚/批准锚/裁定语）"""
        import inspect
        import agent_layer.llm.token_budget as m
        src = inspect.getsource(m)
        assert "防失控" in src, "定位裁定（防失控非省钱）须注释"
        assert "16:39" in src, "09-24 实测锚（6000 在 16:39 耗尽）须注释"
        assert "积分不是约束" in src, "用户裁定语须注释（批准锚）"
        assert "glm-5.2" in src or "0.47" in src, "成本模型（倍率/prompt不计费）须注释"


class TestDeepTopN:
    def test_default_is_ten(self):
        """本卡正题：5→10 全覆盖推荐面（治「理由全是生成中」）"""
        from strategy_layer.agent_gate.deep_review import DeepReview
        assert DeepReview._DEFAULT_DEEP_TOP_N == 10

    def test_reason_annotated(self):
        """扩容依据须注释（用户发现的病象 + 成本核）"""
        import inspect
        from strategy_layer.agent_gate import deep_review
        src = inspect.getsource(deep_review)
        assert "10" in src and ("生成中" in src or "占位" in src), \
            "扩容依据（理由占位病象）须注释在默认值旁"

    def test_budget_covers_full_coverage(self):
        """算账自洽：10只×6专家 满额日需求在新额度内有余量"""
        calls = 10 * 6
        per_call = 500          # 补全 token（prompt 不计费）
        assert TASK_BUDGETS["expert_debate"] >= calls * per_call * 5, \
            "额度须覆盖满额日需求且留足余量（防失控原则）"


class TestDegradationStillObservable:
    """统筹钉：额度耗尽时降级路径仍可观测（放大额度不等于取消保护）"""

    def test_exhaustion_flags_and_clamps(self):
        from agent_layer.llm.token_budget import TokenBudget
        tb = TokenBudget(today_fn=lambda: "2026-09-25")
        tb.record_usage("expert:tech", real_tokens=TASK_BUDGETS["expert_debate"] + 500)
        assert tb.is_exceeded("expert:tech") is True
        assert tb.remaining("expert:tech") == 0, "超耗钳 0（不得负数静默）"

    def test_exhaustion_visible_in_usage_readout(self):
        from agent_layer.llm.token_budget import TokenBudget
        tb = TokenBudget(today_fn=lambda: "2026-09-25")
        tb.record_usage("expert:tech", real_tokens=TASK_BUDGETS["expert_debate"])
        usage = tb.get_all_usage()
        assert usage["expert_debate"]["remaining"] == 0
        assert usage["expert_debate"]["used"] == TASK_BUDGETS["expert_debate"]
