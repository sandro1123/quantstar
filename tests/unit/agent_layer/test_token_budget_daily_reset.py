# -*- coding: utf-8 -*-
"""辩论腿止血 TDD — token 预算按日重置 + 记账口径（统筹 09-24 批·不涉成本部分）

病案（六重证据链，2026-09-24 定谳）：
  辩论六专家腿持续三天全断，日志实文 = `[expert:tech] token budget exceeded`。
  ① expert_debate 桶 6000 耗尽后**永不恢复**（reset() 全树零调用点、无持久化）
  ② 记账口径 `max(1, len(str(result)) // 3)` 把字符数当 token，与真实消耗不同源
  根因定性：信号常量化 + 防线留了没人接线（同族）。

设计要点：**惰性按日自愈** —— 不依赖任何调用点记得调 reset（否则管道改造后
又会重演「防线没人接线」）。每日首次使用自动判定跨日并清零。
时钟经 today_fn 注入 → 测试不耦合真实日期（判例：测试契约勿耦合真实日期）。

本卡只修**不涉成本**的两条；额度数值重设与甲案新增调用待用户裁成本。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_layer.llm.token_budget import TokenBudget, TASK_BUDGETS

DAY1, DAY2 = "2026-09-24", "2026-09-25"


def budget(day=DAY1):
    """确定性时钟：返回可切换日期的预算实例"""
    clock = {"today": day}
    tb = TokenBudget(today_fn=lambda: clock["today"])
    return tb, clock


class TestDailyReset:
    """① 按日重置载体 — 耗尽后跨日必须恢复（惰性自愈，无调用点依赖）"""

    def test_exhausted_then_cross_day_restores(self):
        """耗尽 → 次日首次使用自动恢复 —— 辩论腿三天全断的直接止血点"""
        tb, clock = budget()
        tb.record_usage("expert:tech", TASK_BUDGETS["expert_debate"])
        assert tb.is_exceeded("expert:tech") is True
        clock["today"] = DAY2
        assert tb.is_exceeded("expert:tech") is False, "跨日未自动重置 = 病未修"

    def test_same_day_never_clears(self):
        """同日不重复清零（防同日多次触发互相抹账）"""
        tb, _ = budget()
        tb.record_usage("expert:tech", 3000)
        tb.record_usage("expert:tech", 1000)
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"] - 4000

    def test_remaining_reflects_current_day_after_rollover(self):
        """跨日后 remaining 归满额"""
        tb, clock = budget()
        tb.record_usage("expert:tech", 5000)
        clock["today"] = DAY2
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"]

    def test_explicit_reset_works(self):
        """显式 reset 仍可用（手动干预口）"""
        tb, _ = budget()
        tb.record_usage("expert:tech", TASK_BUDGETS["expert_debate"])
        tb.reset()
        assert tb.is_exceeded("expert:tech") is False

    def test_reset_task_scoped(self):
        """单桶重置不误伤其他桶"""
        tb, _ = budget()
        tb.record_usage("expert:tech", 5000)
        tb.record_usage("multi_agent_review:x", 2000)
        tb.reset_task("expert:tech")
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"]
        assert tb.remaining("multi_agent_review:x") == TASK_BUDGETS["multi_agent_review"] - 2000

    def test_no_today_fn_does_not_crash(self):
        """未注入时钟（生产默认 bj_date）不炸"""
        tb = TokenBudget()
        tb.record_usage("expert:tech", 100)
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"] - 100


class TestUsageAccounting:
    """② 记账口径 — 真实 token 优先，估算为兜底且口径可辨"""

    def test_prefers_real_token_count(self):
        """provider 回传真实 usage → 用真实值（不再拿字符数当 token）"""
        tb, _ = budget()
        tb.record_usage("expert:tech", text="很长的中文返回" * 100, real_tokens=137)
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"] - 137

    def test_falls_back_to_estimate_without_real_tokens(self):
        """无真实 usage → 估算兜底（保持既有行为，且不大于字符数）"""
        tb, _ = budget()
        tb.record_usage("expert:tech", text="abc" * 30)   # 90 字符
        used = TASK_BUDGETS["expert_debate"] - tb.remaining("expert:tech")
        assert 0 < used <= 90

    def test_legacy_positional_tokens_still_works(self):
        """向后兼容：既有 record_usage(task, tokens) 位置参数不破"""
        tb, _ = budget()
        tb.record_usage("expert:tech", 500)
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"] - 500

    def test_no_usage_info_records_zero(self):
        """既无 text 也无 tokens → 记 0（不凭空虚记 1）"""
        tb, _ = budget()
        tb.record_usage("expert:tech")
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"]

    def test_usage_source_real(self):
        tb, _ = budget()
        tb.record_usage("expert:tech", real_tokens=100)
        assert tb.usage_source("expert:tech") == "real"

    def test_usage_source_estimated(self):
        tb, _ = budget()
        tb.record_usage("expert:tech", text="x" * 30)
        assert tb.usage_source("expert:tech") == "estimated"

    def test_usage_source_none_when_untouched(self):
        tb, _ = budget()
        assert tb.usage_source("expert:tech") is None

    def test_usage_source_resets_on_day_rollover(self):
        """跨日后口径标记一并清零（防旧口径残留误导审计）"""
        tb, clock = budget()
        tb.record_usage("expert:tech", text="x" * 30)
        clock["today"] = DAY2
        assert tb.usage_source("expert:tech") is None


class TestBudgetSemanticsUnchanged:
    """额度数值本身不动（待用户裁成本），仅修机制"""

    def test_expert_debate_budget_value_untouched(self):
        # 09-25 用户批全表重设（算账依据锚在 test_budget_table_reset）
        assert TASK_BUDGETS["expert_debate"] == 300000

    def test_bucket_routing_unchanged(self):
        tb, _ = budget()
        assert tb.get_budget("expert:sentiment") == 300000
        assert tb.get_budget("multi_agent_review:x") == TASK_BUDGETS["multi_agent_review"]

    def test_get_all_usage_shape(self):
        tb, _ = budget()
        tb.record_usage("expert:tech", 100)
        usage = tb.get_all_usage()
        assert "expert_debate" in usage
        assert usage["expert_debate"]["used"] == 100
