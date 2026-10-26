# -*- coding: utf-8 -*-
"""模块1b GREEN 判据 — 原料读取器 + rewrite 引擎（渲染测试直接构造数据，不走全链）

原料源 = agent_layer 已留痕的 agent_memory 表（只读零副作用，契约条款3）。
生产词表（AG-03）: 推荐/不推荐/观望/警惕 → 内部语义域 buy/avoid/hold 翻译在 source 层。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.source import load_argument
from report_layer.advisor.models import RecommendationArgument


# ---------- 原料读取器 ----------

@pytest.fixture()
def mem_db():
    """内存库仿 agent_memory 形状（production: code/date/verdict/confidence/reasoning）"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
        "confidence REAL, reasoning TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_memory VALUES ('600000','2026-09-20','推荐',0.72,"
        "'动量因子近20日强势，且估值处于近三年低位，综合判断可买入','2026-09-20 16:12:00')"
    )
    conn.execute(
        "INSERT INTO agent_memory VALUES ('000001','2026-09-20','不推荐',0.55,"
        "'基本面承压且业绩下滑，缺乏核心做多逻辑，建议规避','2026-09-20 16:12:01')"
    )
    yield conn
    conn.close()


class TestSourceLoader:
    def test_loads_recent_rationale(self, mem_db):
        arg = load_argument("600000", "2026-09-20", mem_db)
        assert arg is not None
        assert arg.code == "600000"
        assert "动量因子近20日强势" in arg.reasoning
        assert arg.verdict == "buy"          # 生产词表「推荐」→内部语义域
        assert arg.confidence == pytest.approx(0.72)

    def test_negative_verdict_maps_to_avoid(self, mem_db):
        arg = load_argument("000001", "2026-09-20", mem_db)
        assert arg.verdict == "avoid"

    def test_neutral_verdicts_map_to_hold(self, mem_db):
        for v in ("观望", "警惕"):
            mem_db.execute(
                "INSERT INTO agent_memory VALUES ('300999','2026-09-20',?,0.3,'中性叙述','x')",
                (v,))
        arg = load_argument("300999", "2026-09-20", mem_db)
        assert arg.verdict == "hold"

    def test_unknown_verdict_conservative(self, mem_db):
        """未知词表值 → 保守映射 avoid（禁 KeyError 穿透，空集显式报警同族）"""
        mem_db.execute(
            "INSERT INTO agent_memory VALUES ('300998','2026-09-20','天知道',0.3,'呃','x')")
        arg = load_argument("300998", "2026-09-20", mem_db)
        assert arg.verdict == "avoid"

    def test_picks_latest_row_per_code(self, mem_db):
        """同票多日取最近（created_at/rowid 序）"""
        mem_db.execute(
            "INSERT INTO agent_memory VALUES ('600000','2026-09-19','观望',0.4,"
            "'旧叙述','2026-09-19 16:00:00')")
        arg = load_argument("600000", "2026-09-20", mem_db)
        assert "动量因子近20日强势" in arg.reasoning

    def test_missing_returns_none(self, mem_db):
        assert load_argument("999999", "2026-09-20", mem_db) is None

    def test_absent_tables_return_none_not_raise(self):
        """全新库/未迁移：两张原料表都不存在 → 返回 None 走诚实水位，
        绝不让「缺表」把渲染节炸成「暂无推荐」（Kimi 可读性测试暴露的真缺陷）"""
        import sqlite3
        conn = sqlite3.connect(":memory:")   # 空库：无 agent_memory 也无 rationale 表
        assert load_argument("600000", "2026-09-20", conn) is None

    def test_absent_memory_table_but_rationale_present(self):
        """rationale 表在、agent_memory 缺 → 走 rationale，不因缺表炸"""
        import sqlite3
        from report_layer.advisor.rationale_join import ensure_rationale_table
        conn = sqlite3.connect(":memory:")
        ensure_rationale_table(conn)
        conn.execute("INSERT INTO recommendation_rationale (rationale_id, code, rec_date, "
                     "verdict, confidence, reasoning, expert_name) "
                     "VALUES ('r1','600000','2026-09-20','推荐',0.7,'动量因子强势','chairman')")
        conn.commit()
        arg = load_argument("600000", "2026-09-20", conn)
        assert arg is not None and arg.rationale_id == "r1"

    def test_stale_row_not_used(self, mem_db):
        """date 不匹配（查明日/查过期）→ None（不跨时点拿旧论证充数）"""
        assert load_argument("600000", "2026-09-21", mem_db) is None

    def test_numbers_extracted_for_guard(self, mem_db):
        """reasoning 中的数字被提取进原料 numbers（闸2 封闭域原料面）"""
        arg = load_argument("600000", "2026-09-20", mem_db)
        assert any("20日" in n for n in arg.numbers)
        assert any("三年" in n for n in arg.numbers)


# ---------- rewrite 引擎 ----------

from report_layer.advisor.rewrite import rewrite_to_advisor, translate_terms, RULE_NOTE


class TestRewriteEngine:
    def _arg(self):
        return RecommendationArgument(
            code="600000", verdict="buy", confidence=0.72, position_pct=0.15,
            reasoning="动量因子近20日强势，且估值处于近三年低位，综合判断可买入",
            key_points=["动量因子近20日强势"],
            invalidation_conditions=["跌破10.5元止损"],
            factors=["动量"], numbers=["20日", "三年", "10.5元"],
        )

    def test_translates_internal_terms(self):
        assert "预测力下降" in translate_terms("该因子IC衰减明显")
        # 幂等：已翻译文本不二次变化
        assert translate_terms("该因子预测力下降明显") == "该因子预测力下降明显"

    def test_rewrite_output_passes_guard(self):
        """引擎产出必须过自家守卫（自洽判据）"""
        arg = self._arg()
        result = rewrite_to_advisor(arg, llm=None)
        assert result.sentences
        assert result.degraded is False
        from report_layer.advisor.fidelity import FidelityGuard
        assert FidelityGuard(arg).check(result.sentences).ok is True

    def test_llm_hallucinated_output_degrades_to_raw(self):
        """LLM 改写产出编造依据（守卫拒）→ 降级为原始 reasoning，不静默不编造"""
        arg = self._arg()

        class FakeLLM:
            def call(self, prompt, **kw):
                return "该股基本面向好，历史战绩显示策略胜率八成，建议重仓买入"

        result = rewrite_to_advisor(arg, llm=FakeLLM())
        assert result.degraded is True
        assert arg.reasoning in result.text

    def test_llm_none_falls_back_to_rules(self):
        """LLM 不可用 → 纯规则改写仍产出且过守卫（不回退排名话术）"""
        arg = self._arg()
        result = rewrite_to_advisor(arg, llm=None)
        assert result.sentences
        assert any("动量" in s.text for s in result.sentences)

    def test_honest_pending_when_no_material(self):
        """无原料 → 显式「理由生成中」，绝不回退排名话术充数（诚实水位）"""
        from report_layer.advisor.rewrite import PENDING_LINE
        assert PENDING_LINE and "理由" in PENDING_LINE
