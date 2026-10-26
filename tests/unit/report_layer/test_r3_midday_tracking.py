"""R3: 午报候选跟踪段 + midday_strategy LLM 输入注入 — 红绿测试。

依据: 报告体系重设计稿-C-20260828 §2.4 (午报=候选股跟踪, 一屏读完)
+ 统筹施工令 D4 (午间涨跌停池降附录)。

断言:
1. 跟踪段每候选一行 = 现价 vs 区间状态 (区间内✅/逼近⚠️/尚远✗) + 上午推送状态。
2. 无候选 → 诚实模板 (范式 7)。
3. 午报编排: 跟踪段置顶, 涨跌停池 (midday_alerts) 降附录置尾 (D4)。
4. midday_strategy LLM 输入必含候选股 (修连续性断裂, §2.4 第 3 条)。

hermetic: 内存 sqlite (生命线四表 + stock_info), 实时价 mock, 不碰生产库。
"""
import sqlite3
from unittest.mock import MagicMock, patch

from report_layer.reports.base import ReportContext
from report_layer.reports.midday_brief import MiddayBriefBuilder
from report_layer.sections.midday_candidate_tracking import (
    MiddayCandidateTrackingSection,
)

POOL_COLS = """
    code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
    entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
    entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
    dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
    exit_price REAL, exit_date TEXT, net_return_pct REAL, review_quality TEXT,
    source TEXT, initial_score REAL, entry_plan_source TEXT
"""


def _mkdb() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(f"""
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
        CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT);
    """)
    return con


def _pool(con, code, name, status, **kw):
    defaults = dict(
        added_date="2026-08-28", created_at="2026-08-28 08:00:00",
        entry_plan_date=None, entry_low=None, entry_high=None, valid_until=None,
        entry_price=None, entry_date=None, stop_loss=None, trailing_stop=None,
        dynamic_stop_loss=None, atr_stop=None, take_profit=None,
        exit_price=None, exit_date=None, net_return_pct=None,
        review_quality=None, source="strategy", initial_score=None,
        entry_plan_source=None)
    defaults.update(kw)
    cols = ", ".join(defaults)
    ph = ", ".join("?" * (len(defaults) + 3))
    con.execute(
        f"INSERT INTO stock_pool (code, name, status, {cols}) VALUES ({ph})",
        (code, name, status, *defaults.values()))


def _ctx(con, date="2026-08-28", prices=None):
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = con
    data.db = db
    data.get_realtime_prices.return_value = prices or {}
    return ReportContext(date=date, data_layer=data)


class TestTrackingStatus:
    def _seed(self, con):
        _pool(con, "688349", "三一重能", "pending_entry",
              entry_low=13.79, entry_high=14.15, valid_until="2026-09-01",
              stop_loss=13.26, initial_score=0.37)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("688349", "三一重能", "电气设备"))

    def test_in_range_shows_check(self):
        con = _mkdb(); self._seed(con)
        ctx = _ctx(con, prices={"688349": 14.0})
        r = MiddayCandidateTrackingSection().build(ctx)
        assert r is not None
        assert "三一重能" in r.body
        assert "✅" in r.body  # 区间内

    def test_far_shows_cross(self):
        con = _mkdb(); self._seed(con)
        ctx = _ctx(con, prices={"688349": 15.5})  # 远超上沿
        r = MiddayCandidateTrackingSection().build(ctx)
        assert "✗" in r.body  # 尚远

    def test_near_shows_warn(self):
        con = _mkdb(); self._seed(con)
        ctx = _ctx(con, prices={"688349": 13.70})  # 略低于下沿 1% 内
        r = MiddayCandidateTrackingSection().build(ctx)
        assert "⚠️" in r.body  # 逼近

    def test_push_status_shown(self):
        con = _mkdb(); self._seed(con)
        con.execute(
            "INSERT INTO intraday_checks (code, check_type, current_price, "
            "trigger_price, status, detail, check_time) VALUES "
            "('688349','entry',14.0,NULL,'triggered','触价','2026-08-28 10:23:00')")
        ctx = _ctx(con, prices={"688349": 14.0})
        r = MiddayCandidateTrackingSection().build(ctx)
        assert "已推送" in r.body

    def test_no_push_shows_not_triggered(self):
        con = _mkdb(); self._seed(con)
        ctx = _ctx(con, prices={"688349": 14.0})
        r = MiddayCandidateTrackingSection().build(ctx)
        assert "未触发" in r.body


class TestHonestEmpty:
    def test_empty_pool_honest(self):
        con = _mkdb()
        ctx = _ctx(con)
        r = MiddayCandidateTrackingSection().build(ctx)
        assert r is not None
        assert "无候选" in r.body or "只看不动" in r.body

    def test_no_data_layer_honest(self):
        ctx = ReportContext(date="2026-08-28", data_layer=None)
        r = MiddayCandidateTrackingSection().build(ctx)
        assert r is not None
        assert "数据缺口" in r.body or "无候选" in r.body


class TestMiddayComposition:
    def test_tracking_first_alerts_last(self):
        """D4: 跟踪段置顶, 涨跌停池降附录置尾。"""
        names = [s.name for s in MiddayBriefBuilder().sections()]
        assert names[0] == "midday_candidate_tracking"
        assert names[-1] == "midday_alerts"


class TestStrategyPromptInjection:
    """§2.4 第 3 条: midday_strategy LLM 输入必须注入候选股清单。"""

    def test_prompt_contains_candidate_codes(self):
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              entry_low=13.79, entry_high=14.15, valid_until="2026-09-01",
              stop_loss=13.26, initial_score=0.37)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("688349", "三一重能", "电气设备"))
        ctx = _ctx(con, prices={"688349": 14.0})

        llm = MagicMock()
        llm.is_available.return_value = True
        llm.call.return_value = "方向判断: 偏多"
        ctx.llm_service = llm

        from report_layer.sections.midday_strategy import MiddayStrategySection
        with patch("report_layer.sections.indices.fetch_realtime_indices",
                   return_value=[{"name": "上证指数", "price": 3200, "change_pct": 0.5}]):
            MiddayStrategySection().build(ctx)

        assert llm.call.called
        prompt = llm.call.call_args[0][0]
        assert "688349" in prompt  # 候选股必在 prompt
        assert "三一重能" in prompt
