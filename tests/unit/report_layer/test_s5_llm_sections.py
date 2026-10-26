"""F5.3 LLM 增强 5 Section 测试 — 每 section 附 LLM mock 成功 + 降级两路径

约定: LLM 失败/不可用 → 规则降级, 报告永不阻塞; sources 标注来源。
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from report_layer.reports.base import ReportContext
from report_layer.sections._llm_helper import clean_markdown, llm_call
from report_layer.sections.conclusion import ConclusionSection
from report_layer.sections.exit_review import ExitReviewSection
from report_layer.sections.overnight_market import OvernightMarketSection
from report_layer.sections.risk_tips import RiskTipsSection
from report_layer.sections.sector_analysis import SectorAnalysisSection


class FakeLLM:
    """最小 LLMService 替身: is_available + call"""

    def __init__(self, reply=None, available=True, raise_on_call=False):
        self.reply = reply
        self._available = available
        self._raise = raise_on_call
        self.calls = []

    def is_available(self):
        return self._available

    def call(self, prompt, system="", temperature=0.7, max_tokens=500,
             call_point="llm_service"):
        self.calls.append({"prompt": prompt, "call_point": call_point})
        if self._raise:
            raise RuntimeError("provider down")
        return self.reply


class TestLLMHelper:
    def test_no_llm_returns_none(self):
        ctx = ReportContext(date="2026-08-17")
        assert llm_call(ctx, "p", call_point="t") is None

    def test_unavailable_returns_none(self):
        llm = FakeLLM(reply="x", available=False)
        ctx = ReportContext(date="2026-08-17", llm_service=llm)
        assert llm_call(ctx, "p", call_point="t") is None
        assert llm.calls == []

    def test_call_exception_returns_none(self):
        llm = FakeLLM(raise_on_call=True)
        ctx = ReportContext(date="2026-08-17", llm_service=llm)
        assert llm_call(ctx, "p", call_point="t") is None

    def test_success_returns_cleaned_text_with_call_point(self):
        llm = FakeLLM(reply="## 标题内容")
        ctx = ReportContext(date="2026-08-17", llm_service=llm)
        out = llm_call(ctx, "p", call_point="conclusion")
        assert out == "标题内容"
        assert llm.calls[0]["call_point"] == "conclusion"

    def test_timeout_returns_none(self):
        class SlowLLM(FakeLLM):
            def call(self, *a, **kw):
                time.sleep(1.5)
                return "late"
        ctx = ReportContext(date="2026-08-17", llm_service=SlowLLM(reply="late"))
        assert llm_call(ctx, "p", call_point="t", timeout_s=1) is None

    def test_clean_markdown_strips_headers(self):
        assert clean_markdown("### 【结论】观望") == "【结论】观望"
        assert clean_markdown("  普通文本  ") == "普通文本"


class TestConclusionSectionLLM:
    def _ctx(self, llm):
        from common.database import Database
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (date TEXT, change_pct REAL)")
        # 10 只: 7 涨 (其中 4 只涨停) → 规则路径应为"值得买"
        rows = [("2026-08-17", p) for p in
                [9.9, 9.85, 9.8, 9.82, 2.0, 1.0, 0.5, -1.0, -2.0, -3.0]]
        conn.executemany("INSERT INTO daily_klines VALUES (?, ?)", rows)
        conn.commit()
        dl = MagicMock()
        dl.db = db
        return ReportContext(date="2026-08-17", data_layer=dl, llm_service=llm)

    def test_llm_success_uses_llm_body(self):
        reply = "【结论】观望\n【依据】量能不足\n【操作】轻仓\n【风险】高位回落"
        result = ConclusionSection().build(self._ctx(FakeLLM(reply=reply)))
        assert "【结论】观望" in result.body
        assert "仅供参考" in result.body
        assert result.sources == ["LLM"]

    def test_llm_failure_falls_back_to_rules(self):
        result = ConclusionSection().build(self._ctx(FakeLLM(raise_on_call=True)))
        # 70% 上涨但涨停仅 4 只 (<30) → 规则分支"可以考虑"
        assert "可以考虑" in result.body
        assert "仅供参考" in result.body
        assert result.sources == ["rule-based"]

    def test_no_llm_service_falls_back(self):
        result = ConclusionSection().build(self._ctx(None))
        assert "结论" in result.body
        assert result.sources == ["rule-based"]


class TestSectorAnalysisLLM:
    def _ctx(self, llm):
        klines = (
            [SimpleNamespace(sector="半导体", change_pct=3.0, volume_ratio=2.0, amount=1e8)] * 3
            + [SimpleNamespace(sector="银行", change_pct=-1.5, volume_ratio=0.8, amount=5e7)] * 2
        )
        dl = MagicMock()
        dl.snapshot.return_value = SimpleNamespace(klines=klines)
        return ReportContext(date="2026-08-17", data_layer=dl, llm_service=llm)

    def test_llm_success_appends_comment(self):
        result = SectorAnalysisSection().build(self._ctx(FakeLLM(reply="轮动向科技集中")))
        assert "🧠 研判: 轮动向科技集中" in result.body
        assert "强势板块" in result.body  # 规则统计仍在
        assert result.sources == ["rule-based", "LLM"]

    def test_llm_failure_rule_only(self):
        result = SectorAnalysisSection().build(self._ctx(FakeLLM(reply=None)))
        assert "🧠" not in result.body
        assert "强势板块" in result.body
        assert result.sources == ["rule-based"]


class TestExitReviewLLM:
    def _ctx(self, llm):
        el = MagicMock()
        el.stock_pool.get_by_status.return_value = [
            {"code": "001232", "name": "嘉立创", "exit_date": "2026-08-15",
             "net_return_pct": -5.52, "holding_days_actual": 2, "exit_reason": "止损"},
            {"code": "002543", "name": "万和电气", "exit_date": "2026-08-14",
             "net_return_pct": 4.95, "holding_days_actual": 3, "exit_reason": "止盈"},
        ]
        el.post_exit_tracker = None
        return ReportContext(date="2026-08-17", execution_layer=el, llm_service=llm)

    def test_llm_success_appends_recap(self):
        result = ExitReviewSection().build(self._ctx(FakeLLM(reply="多为止损离场, 追高所致")))
        assert "🧠 复盘: 多为止损离场, 追高所致" in result.body
        assert result.sources == ["rule-based", "LLM"]

    def test_llm_failure_rule_only(self):
        result = ExitReviewSection().build(self._ctx(FakeLLM(available=False)))
        assert "🧠" not in result.body
        assert "退出回顾" in result.body
        assert result.sources == ["rule-based"]


class TestOvernightMarketLLM:
    def _section(self, llm):
        section = OvernightMarketSection()
        section._fetch_overnight = lambda: {
            "dow": 45000.0, "dow_pct": 1.2,
            "nasdaq": 21000.0, "nasdaq_pct": -0.8,
        }
        return section

    def test_llm_success_appends_interpretation(self):
        ctx = ReportContext(date="2026-08-17", llm_service=FakeLLM(reply="外盘偏暖利好开盘"))
        result = self._section(None).build(ctx)
        assert "🧠 解读: 外盘偏暖利好开盘" in result.body
        assert result.sources == ["akshare", "LLM"]

    def test_llm_failure_list_only(self):
        ctx = ReportContext(date="2026-08-17", llm_service=FakeLLM(reply=None))
        result = self._section(None).build(ctx)
        assert "🧠" not in result.body
        assert "道琼斯" in result.body
        assert result.sources == ["akshare"]


class TestRiskTipsLLM:
    def _ctx(self, llm):
        klines = ([SimpleNamespace(change_pct=-4.0)] * 4
                  + [SimpleNamespace(change_pct=1.0)] * 6)
        dl = MagicMock()
        dl.snapshot.return_value = SimpleNamespace(klines=klines)
        return ReportContext(date="2026-08-17", data_layer=dl, llm_service=llm)

    def test_llm_success_appends_tips(self):
        result = RiskTipsSection().build(self._ctx(FakeLLM(reply="注意高位股补跌")))
        assert "🧠 注意高位股补跌" in result.body
        assert "⚠️" in result.body  # 规则告警仍在 (4/10 跌超3%)
        assert result.sources == ["rule-based", "LLM"]

    def test_llm_failure_rule_only(self):
        result = RiskTipsSection().build(self._ctx(FakeLLM(raise_on_call=True)))
        assert "🧠" not in result.body
        assert "⚠️" in result.body
        assert result.sources == ["rule-based"]

    def test_no_risk_rule_line(self):
        klines = [SimpleNamespace(change_pct=1.0)] * 10
        dl = MagicMock()
        dl.snapshot.return_value = SimpleNamespace(klines=klines)
        ctx = ReportContext(date="2026-08-17", data_layer=dl, llm_service=None)
        result = RiskTipsSection().build(ctx)
        assert "✅ 暂无显著风险" in result.body
        assert result.sources == ["rule-based"]
