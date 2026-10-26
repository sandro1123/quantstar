# -*- coding: utf-8 -*-
"""模块1b 接入判据 — stock_picks/tomorrow_picks 经 advisor 加工。

渲染测试直接构造 context（不走全链，判例：渲染/话术测试勿走全链）。

22:05 窗合案重锚（tomorrow_picks 两条）：免责句归报告级 risk_compliance
承载（段级不再重复）；无 agent 论证的兜底文案归 _pick_reason 单点闸
（批复裁决3），排名话术永不进卡这一核心不变量两条均保留锚。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.sections.base import SectionResult
from report_layer.advisor import DISCLAIMER


def make_context(mem_rows, pool_rows=None, date="2026-09-20",
                 signal_codes=None):
    """构造 context：data_layer.db=内存sqlite，strategy_layer 返回固定signals。
    signal_codes 独立于 mem_rows（可测「有信号无原料」的诚实水位）"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
                 "confidence REAL, reasoning TEXT, created_at TEXT)")
    for r in mem_rows:
        conn.execute("INSERT INTO agent_memory VALUES (?,?,?,?,?,?)", r)

    class FakeDB:
        def get_connection(self):
            return conn

    codes = signal_codes if signal_codes is not None else [r[0] for r in mem_rows]
    signals = [SimpleNamespace(code=c, action="buy", strength=0.8) for c in codes]

    class FakeStrategy:
        def run_daily(self, date):
            return SimpleNamespace(signals=signals, regime="")

    ctx = SimpleNamespace(
        date=date,
        data_layer=SimpleNamespace(db=FakeDB()),
        strategy_layer=FakeStrategy(),
        llm_service=None,
        _cache={},
    )
    return ctx


# ---------- stock_picks 接入 ----------

class TestStockPicksAdvisor:
    def test_picks_shows_rationale_lines(self):
        """有原料：精选节带「为什么」论证行（翻译自 reasoning，非排名话术）"""
        from report_layer.sections.stock_picks import StockPicksSection
        ctx = make_context([
            ("600000", "2026-09-20", "推荐", 0.72,
             "动量因子近20日强势，且估值处于近三年低位，综合判断可买入",
             "2026-09-20 16:12:00"),
        ])
        result = StockPicksSection().build(ctx)
        assert result is not None
        assert "600000" in result.body
        assert "为什么" in result.body
        assert "动量" in result.body      # 论证关键词进入正文
        assert "综合得分" not in result.body  # 排名话术不再单独充数

    def test_picks_honest_pending_without_material(self):
        """无原料（agent_memory 无此票当日行）：显式「理由生成中」，不编造不回退排名话术"""
        from report_layer.sections.stock_picks import StockPicksSection
        ctx = make_context(mem_rows=[], signal_codes=["999999"])
        result = StockPicksSection().build(ctx)
        assert "999999" in result.body
        assert "理由生成中" in result.body

    def test_picks_disclaimer_in_body(self):
        """免责句落在推荐正文（段尾位置）"""
        from report_layer.sections.stock_picks import StockPicksSection
        ctx = make_context([
            ("600000", "2026-09-20", "推荐", 0.72,
             "动量因子近20日强势，综合判断可买入", "2026-09-20 16:12:00"),
        ])
        result = StockPicksSection().build(ctx)
        assert DISCLAIMER in result.body


# ---------- tomorrow_picks 接入 ----------

class TestTomorrowPicksAdvisor:
    def _ctx_with_pool(self, mem_rows, pool_rows, date="2026-09-20"):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
                     "confidence REAL, reasoning TEXT, created_at TEXT)")
        for r in mem_rows:
            conn.execute("INSERT INTO agent_memory VALUES (?,?,?,?,?,?)", r)
        conn.execute("CREATE TABLE stock_pool (code TEXT, name TEXT, entry_low REAL, "
                     "entry_high REAL, stop_loss REAL, take_profit REAL, added_reason TEXT, "
                     "status TEXT, valid_until TEXT, initial_score REAL)")
        for r in pool_rows:
            conn.execute(
                "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r[0], r[1], 10.0, 10.5, 9.5, 12.0, "候选池第1位", "watch", None, 0.9))

        class FakeDB:
            def get_connection(self):
                return conn

        ctx = SimpleNamespace(date=date, data_layer=SimpleNamespace(db=FakeDB()),
                              strategy_layer=SimpleNamespace(), llm_service=None,
                              _cache={})
        return ctx

    def test_tomorrow_prefers_agent_rationale_over_rank_phrase(self):
        """agent_memory 有论证 → 用加工后论证，排名话术「候选池第X位」被替换"""
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection
        ctx = self._ctx_with_pool(
            [("600000", "2026-09-20", "推荐", 0.72,
              "动量因子近20日强势，且估值处于近三年低位", "2026-09-20 16:12:00")],
            [("600000", "浦发银行",)])
        result = TomorrowPicksSection().build(ctx)
        assert "为什么选它" in result.body
        assert "动量" in result.body
        assert "候选池第" not in result.body

    def test_tomorrow_honest_when_no_agent_rationale(self):
        """无 agent 论证 → _pick_reason 诚实占位（22:05 窗合案重锚）。

        旧锚「综合板块热度」锁的是 Kimi 重构前的四因占位文案；批复裁决3
        把兜底口径收敛到 _pick_reason 单点闸：added_reason 命中排名模板
        （夹具恰为「候选池第1位」）→ 显式「量化评分入选，个股依据生成中」，
        排名话术永不进卡。合案后此语义不变，锚改为新文案。
        """
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection
        ctx = self._ctx_with_pool(
            [],
            [("600000", "浦发银行",)])
        result = TomorrowPicksSection().build(ctx)
        assert "依据生成中" in result.body      # _pick_reason 诚实占位（裁决3）
        assert "候选池第" not in result.body     # 排名话术永不进卡（核心不变量）

    def test_tomorrow_compliance_at_report_level(self):
        """免责句归宿改为报告级（22:05 窗合案重锚）。

        旧锚 pin 每只推荐段尾带 DISCLAIMER —— 那是 1b 在 Kimi 结构重构前
        的设计；重构后合规行由 risk_compliance 段「每报必带」统一承载
        （test_report_layer/test_s5_unified_pipeline 等 5 处已锚报告级口径）。
        本测试改为确认：tomorrow_picks 段体不重复免责句（归 risk_compliance），
        报告级锚点由既有测试守护，不在此重复。
        """
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection
        ctx = self._ctx_with_pool(
            [("600000", "2026-09-20", "推荐", 0.72, "动量因子近20日强势", "2026-09-20 16:12:00")],
            [("600000", "浦发银行",)])
        result = TomorrowPicksSection().build(ctx)
        assert "动量" in result.body             # advisor 链正常产出
        assert DISCLAIMER not in result.body     # 段级不带 → 报告级 risk_compliance 承载
