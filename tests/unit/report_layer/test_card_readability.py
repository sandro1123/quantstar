# -*- coding: utf-8 -*-
"""飞书卡片可读性施工 (fix/report-readability) — 红绿测试。

判例: 渲染测试别走全链 — 直接构造 sections/Message dict 测 formatter 输出。
断言 (对应施工卡):
1. 卡片标题带日期 + subtitle 置顶一句话结论 + 合规免责句提到卡片头。
2. _severity_color: info → 非红 (红只留 critical)。
3. 行动段 (明日推荐) 首行标「模拟盘」。
4. 卡片正文剥离「来源:」尾注。
5. 晚报编排: 持仓信息 1 次 / 大盘信息 1 次, 行动类段靠前, 合规段仍在尾。
6. 内部编号 (生命线 ID code@date / OutboxStore.mark_cleared) 不进卡片正文。
"""
import sqlite3
from unittest.mock import MagicMock, patch

from delivery_layer.message import Message
from report_layer.formatters.feishu_card import FeishuCardFormatter
from report_layer.reports.base import ReportContext
from report_layer.reports.evening_report import EveningReportBuilder

POOL_COLS = """
    code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
    entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
    entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
    dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
    exit_price REAL, exit_date TEXT, net_return_pct REAL, review_quality TEXT,
    source TEXT, initial_score REAL, entry_plan_source TEXT,
    exit_layer TEXT, exit_reason TEXT, exit_reason_detail TEXT,
    total_return_pct REAL, holding_days_actual INTEGER, review_note TEXT,
    last_price REAL, unrealized_pnl_pct REAL, holding_days INTEGER,
    added_reason TEXT, entry_reason TEXT
"""

LIFELINE_SCHEMA = f"""
    CREATE TABLE stock_pool ({POOL_COLS});
    CREATE TABLE action_records (
        id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
        price REAL, reason TEXT, created_at TEXT);
    CREATE TABLE intraday_checks (
        id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
        current_price REAL, trigger_price REAL, status TEXT,
        detail TEXT, check_time TEXT);
    CREATE TABLE recommendation_retrospect (
        prediction_id INTEGER, code TEXT, rec_date TEXT, verdict TEXT,
        hit_5d INTEGER, hit_10d INTEGER,
        fwd_return_5d REAL, fwd_return_10d REAL);
"""


def _mkdb() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(LIFELINE_SCHEMA)
    return con


def _mkctx(con, date="2026-09-20") -> ReportContext:
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = con
    data.db = db
    return ReportContext(date=date, data_layer=data)


def _pool(con, code, name, status, **kw):
    defaults = dict(
        added_date="2026-09-19", created_at="2026-09-19 08:00:00",
        entry_plan_date=None, entry_low=None, entry_high=None, valid_until=None,
        entry_price=None, entry_date=None, stop_loss=None, trailing_stop=None,
        dynamic_stop_loss=None, atr_stop=None, take_profit=None,
        exit_price=None, exit_date=None, net_return_pct=None,
        review_quality=None, source="strategy", initial_score=None,
        entry_plan_source=None, exit_layer=None, exit_reason=None,
        exit_reason_detail=None, total_return_pct=None,
        holding_days_actual=None, review_note=None, last_price=None,
        unrealized_pnl_pct=None, holding_days=None,
        added_reason=None, entry_reason=None)
    defaults.update(kw)
    cols = ", ".join(defaults)
    ph = ", ".join("?" * (len(defaults) + 3))
    con.execute(
        f"INSERT INTO stock_pool (code, name, status, {cols}) VALUES ({ph})",
        (code, name, status, *defaults.values()))


def _evening_msg(sections=None, severity="info"):
    return Message(
        content={"sections": sections or [
            {"name": "tomorrow_picks", "title": "📌 明日推荐",
             "body": "模拟盘 · 明日推荐（1 只）\n◆ 测试股(000001)",
             "sources": ["stock_pool"]},
            {"name": "risk_compliance", "title": "⚠️ 风险提示",
             "body": "暂无显著风险", "sources": ["rule-based"]},
        ], "date": "2026-09-20"},
        content_type="evening_report",
        title="📊 QuantStar 晚报",
        severity=severity,
    )


class TestSeverityColorContract:
    """_severity_color 调用面只有 feishu_card.py:18 (format) 一处; 契约为
    info→非红 / warning→yellow / critical→red (红只留 critical)。"""

    def test_info_card_header_not_red(self):
        card = FeishuCardFormatter().format(_evening_msg(severity="info"))
        assert card["card"]["header"]["template"] != "red"

    def test_warning_card_header_yellow(self):
        card = FeishuCardFormatter().format(_evening_msg(severity="warning"))
        assert card["card"]["header"]["template"] == "yellow"

    def test_critical_card_header_red(self):
        card = FeishuCardFormatter().format(_evening_msg(severity="critical"))
        assert card["card"]["header"]["template"] == "red"

    def test_unknown_severity_not_red(self):
        card = FeishuCardFormatter().format(_evening_msg(severity="unknown"))
        assert card["card"]["header"]["template"] != "red"


class TestCardHeaderReadability:
    def test_title_contains_date(self):
        card = FeishuCardFormatter().format(_evening_msg())
        assert "2026-09-20" in card["card"]["header"]["title"]["content"]

    def test_subtitle_is_one_line_conclusion(self):
        card = FeishuCardFormatter().format(_evening_msg())
        subtitle = card["card"]["header"]["subtitle"]["content"]
        assert "今日动作" in subtitle
        assert "可关注" in subtitle

    def test_subtitle_contains_compliance_disclaimer(self):
        card = FeishuCardFormatter().format(_evening_msg())
        subtitle = card["card"]["header"]["subtitle"]["content"]
        assert "模拟盘研究性质" in subtitle
        assert "非投资建议" in subtitle


class TestCardBodyCleanup:
    def test_sources_footnote_stripped(self):
        # 「来源: stock_pool · pipeline_runs」这类尾注不进卡片正文
        card = FeishuCardFormatter().format(_evening_msg())
        contents = [e["text"]["content"] for e in card["card"]["elements"]
                    if e.get("tag") == "div"]
        assert not any("来源:" in c for c in contents)

    def test_sources_key_still_accepted(self):
        # sections 带 sources 键不崩 (向后兼容, 只是不渲染)
        card = FeishuCardFormatter().format(_evening_msg())
        assert card["card"]["elements"]


class TestTomorrowPicksSimTag:
    def test_action_section_first_line_marks_sim(self):
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection

        con = _mkdb()
        _pool(con, "000001", "测试股", "pending_entry",
              entry_low=9.5, entry_high=10.0, stop_loss=9.0,
              take_profit=11.0, added_reason="低估值",
              valid_until="2026-09-30", initial_score=0.8)
        ctx = _mkctx(con)
        ctx.strategy_layer = MagicMock()
        r = TomorrowPicksSection().build(ctx)
        assert r is not None
        assert "模拟盘" in r.body.splitlines()[0]
        # 措辞表 (F-20260828): 止损 → 参考卖出线, 与 DM 卡对齐
        assert "止损" not in r.body
        assert "参考卖出线" in r.body


class TestEveningCompositionDeduplicated:
    def test_position_info_appears_once(self):
        # TASK_20260922_01 Phase B (批复裁决2): 持仓管家段移出编排 — 持有组改
        # 三行式持续监控, 由 evening_storyline 承担; 持仓信息全卡仍只出现 1 次
        # (断言从「只剩 position_steward」翻新为「零持仓段 + 故事线独担」)。
        names = [s.name for s in EveningReportBuilder().sections()]
        position_like = [n for n in names if "position" in n or n == "portfolio"]
        assert position_like == []
        assert "evening_storyline" in names

    def test_market_info_appears_once(self):
        names = [s.name for s in EveningReportBuilder().sections()]
        market_like = [n for n in names if "market" in n]
        assert market_like == ["market_wrap"]

    def test_action_sections_early_compliance_last(self):
        names = [s.name for s in EveningReportBuilder().sections()]
        assert names[0] == "yesterday_picks_review"
        assert names[-1] == "risk_compliance"
        # 行动类段 (明日推荐) 尽量靠前: 在前 4 段内
        assert names.index("tomorrow_picks") < 4


class TestInternalIdsStripped:
    def test_storyline_no_lifeline_id(self):
        from report_layer.sections.evening_storyline import \
            EveningStorylineSection

        con = _mkdb()
        _pool(con, "000002", "万科A", "pending_entry",
              added_date="2026-09-10", entry_low=8.0, entry_high=8.5,
              valid_until="2026-09-19", stop_loss=7.5)  # 已过期 → 作废计数行
        r = EveningStorylineSection().build(_mkctx(con))
        assert r is not None
        # TASK_20260922_01 Phase B: 过期组聚合为一行计数 (批复裁决2 少即是多),
        # 「万科A」逐只行按裁决不再出现; 诚实水位保留在「作废」计数行。
        assert "作废" in r.body
        assert "1 只候选到期未触发" in r.body
        assert "生命线" not in r.body
        assert "000002@" not in r.body  # 生命线 ID (code@date) 不进卡片正文

    def test_action_candidates_no_lifeline_id(self):
        from report_layer.sections.action_candidates import \
            ActionCandidatesSection

        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              entry_low=13.79, entry_high=14.15, valid_until="2026-09-30",
              stop_loss=13.26, initial_score=0.85)
        r = ActionCandidatesSection().build(_mkctx(con))
        assert r is not None
        assert "688349@" not in r.body  # 生命线 ID (code@date) 不进卡片正文
        assert "生命线" not in r.body

    def test_avoid_cards_no_lifeline_id(self):
        from report_layer.sections.avoid_cards import AvoidCardsSection

        con = _mkdb()
        _pool(con, "600022", "ST测试", "watch",
              entry_low=2.0, entry_high=2.2, valid_until="2026-09-30",
              stop_loss=1.8)
        r = AvoidCardsSection().build(_mkctx(con))
        assert r is not None
        assert "600022@" not in r.body  # 生命线 ID (code@date) 不进卡片正文
        assert "生命线" not in r.body

    def test_outbox_watermark_no_class_name(self):
        from report_layer.sections.outbox_watermark import \
            OutboxWatermarkSection

        fake_entries = [{
            "ts": "2026-09-20T07:30:00", "content_type": "evening_report",
            "title": "晚报",
            "attempts": [{"channel": "feishu", "error": "webhook 超时"}],
        }]
        with patch("report_layer.sections.outbox_watermark.OutboxStore") as m:
            m.return_value.pending.return_value = fake_entries
            r = OutboxWatermarkSection().build(
                ReportContext(date="2026-09-20"))
        assert r is not None
        assert "OutboxStore" not in r.body
        assert "mark_cleared" not in r.body
        assert "运维" in r.body  # 重投指引改用户/运维可读措辞
