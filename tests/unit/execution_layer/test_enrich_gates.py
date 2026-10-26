# -*- coding: utf-8 -*-
"""enrich 三道前置闸 TDD（统筹裁定甲，09-25 实证前预警）

■ 病案（实证前用昨日数据模拟捕获，非事后复盘）：
  enrich 回填的是 agent_memory 回落料（recommendation_rationale 全表 0 行——
  1a 落库腿在 main 但从没产出），而 agent_memory 存的是 agent 自己发起的分析
  （news/复盘/盘中），**不是为这次推荐做的辩论** → 拿 A 场景判断解释 B 场景动作。
  09-24 三票模拟实测：
    001319 → 回填「规则模式: composite_score=0.12「未调LLM」」（把内部状态当理由，
             比排名话术更糟：排名话术至少诚实）
    688668 → 回填「[数学判断保守采纳: 不推荐]【观望】\n综合评分-0.3047…」
             （内部标记泄漏 + verdict=avoid 却配 buy 入池动作）
    603322 → 真 LLM 理由（唯一正常的）

■ 守卫为何没拦：忠实性守卫守的是「加工不改原料结论」，缺的是「原料适不适合
  这个用途」——后者是接线层责任（本卡补）。

■ 三闸（全 restrictive：拒 = 保持原样走下游诚实占位闸，失败模式 = 现状）：
  闸A 动作一致：入池 action=buy 而原料 verdict ∈ {avoid, hold} → 拒
               （「推荐买入…理由：建议观望」比没理由更糟，违背 REC「有理」原则）
  闸B 原料质量：reasoning 含规则腿/未调 LLM 标记 → 不算真理由 → 拒
  闸C 标记清洗：内部标记 [..]/【..】/换行 → 清洗；清洗后空或仍含标记 → 拒
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from execution_layer.core.enrich_reason import (
    enrich_reason, passes_action_gate, is_rule_leg_artifact, sanitize_internal_marks,
)

ORIG = "候选池第 1/11 位, 综合得分 0.755"


def _db(rows):
    """rows: [(code, date, verdict, reasoning)] → 内存 agent_memory"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
                 "confidence REAL, reasoning TEXT, created_at TEXT)")
    for code, d, v, rs in rows:
        conn.execute("INSERT INTO agent_memory VALUES (?,?,?,0.6,?,'x')", (code, d, v, rs))
    conn.commit()
    return conn


class TestGateA_ActionConsistency:
    """闸A：入池动作与原料判断必须相容"""

    @pytest.mark.parametrize("verdict", ["不推荐", "观望", "警惕", "卖出", "规避"])
    def test_buy_with_non_buy_rationale_rejected(self, verdict):
        """buy 入池 + 非买入判断 → 拒（不写自相矛盾的理由）"""
        assert passes_action_gate("buy", verdict) is False

    @pytest.mark.parametrize("verdict", ["推荐", "买入"])
    def test_buy_with_buy_rationale_passes(self, verdict):
        assert passes_action_gate("buy", verdict) is True

    def test_unknown_action_conservative_pass(self):
        """非 buy 动作（sell/hold 等）不在本闸管辖 → 放行交后续闸"""
        assert passes_action_gate("sell", "不推荐") is True

    def test_empty_verdict_rejected(self):
        """判断缺失 → 拒（不拿无判断的料当理由）"""
        assert passes_action_gate("buy", "") is False
        assert passes_action_gate("buy", None) is False


class TestGateB_MaterialQuality:
    """闸B：规则腿产物不算真理由"""

    @pytest.mark.parametrize("text", [
        "规则模式: composite_score=0.12「未调LLM」",
        "规则模式: composite_score=0.5",
        "未调LLM",
        "[规则腿] 综合评分偏空",
    ])
    def test_rule_leg_artifacts_detected(self, text):
        assert is_rule_leg_artifact(text) is True

    @pytest.mark.parametrize("text", [
        "中长期动量与基本面质量优异，但短期出现严重超买与量价背离",
        "动量因子近20日强势，估值处于近三年低位",
    ])
    def test_real_llm_reason_not_flagged(self, text):
        assert is_rule_leg_artifact(text) is False

    def test_empty_is_artifact(self):
        assert is_rule_leg_artifact("") is True
        assert is_rule_leg_artifact(None) is True


class TestGateC_MarkSanitize:
    """闸C：内部标记清洗"""

    def test_strips_bracket_marks(self):
        out = sanitize_internal_marks("[数学判断保守采纳: 不推荐] 【观望】\n综合评分-0.3047微幅偏空")
        assert "[" not in out and "【" not in out
        assert "\n" not in out
        assert "综合评分" in out

    def test_keeps_clean_text_intact(self):
        t = "动量因子近20日强势，估值处于近三年低位"
        assert sanitize_internal_marks(t) == t

    def test_all_marks_leaves_empty(self):
        """全是标记 → 清洗后空（调用方据此拒）"""
        assert sanitize_internal_marks("[数学判断保守采纳: 不推荐]【观望】").strip() == ""


class TestThreeStockRegression:
    """09-24 三票实测回归（统筹指定判据）"""

    def test_001319_rule_artifact_rejected(self):
        """001319: 「未调LLM」规则腿产物 → 拒，保持原 added_reason"""
        conn = _db([("001319", "2026-09-24", "观望",
                     "规则模式: composite_score=0.12「未调LLM」")])
        out = enrich_reason(None, "001319", "2026-09-24", ORIG, conn=conn, action="buy")
        assert out == ORIG, f"应拒回填，实得 {out!r}"

    def test_688668_internal_marks_and_verdict_mismatch_rejected(self):
        """688668: 内部标记 + avoid verdict 配 buy 动作 → 拒"""
        conn = _db([("688668", "2026-09-24", "不推荐",
                     "[数学判断保守采纳: 不推荐] 【观望】\n综合评分-0.3047微幅偏空")])
        out = enrich_reason(None, "688668", "2026-09-24", ORIG, conn=conn, action="buy")
        assert out == ORIG, f"应拒回填，实得 {out!r}"

    def test_603322_still_rejected_by_action_gate(self):
        """603322: 理由是真 LLM 的，但 verdict=观望 配 buy → 闸A 仍拒
        （诚实结论：昨日三票在新闸下全拒——因为它们的料本就不是为这次推荐做的辩论）"""
        conn = _db([("603322", "2026-09-24", "观望",
                     "中长期动量与基本面质量优异，但短期出现严重超买与量价背离")])
        out = enrich_reason(None, "603322", "2026-09-24", ORIG, conn=conn, action="buy")
        assert out == ORIG

    def test_true_buy_rationale_still_replaces(self):
        """正路不被误伤：verdict=推荐 + 真 LLM 理由 → 真替换排名话术（A13 验收面）"""
        conn = _db([("600000", "2026-09-25", "推荐",
                     "动量因子近20日强势，估值处于近三年低位，资金面持续净流入")])
        out = enrich_reason(None, "600000", "2026-09-25", ORIG, conn=conn, action="buy")
        assert out != ORIG, "有真理由时必须替换排名话术"
        assert "候选池第" not in out
        assert "动量" in out


class TestNeverBlocks:
    """铁律#12 不变：任何情况不抛、不阻断入池"""

    def test_gate_error_returns_original(self, monkeypatch):
        monkeypatch.setattr("execution_layer.core.enrich_reason.passes_action_gate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        conn = _db([("600000", "2026-09-25", "推荐", "真理由")])
        out = enrich_reason(None, "600000", "2026-09-25", ORIG, conn=conn, action="buy")
        assert out == ORIG

    def test_no_action_arg_backward_compatible(self):
        """action 缺省（既有调用点不传）→ 不因缺参炸，闸A 放行交后续闸"""
        conn = _db([("600000", "2026-09-25", "推荐", "动量因子近20日强势")])
        out = enrich_reason(None, "600000", "2026-09-25", ORIG, conn=conn)
        assert out != ORIG
