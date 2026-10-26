"""R3: 晚报生命线重构 — 红绿测试。

依据: 报告体系重设计稿-C-20260828 §2.5 (晚报=今日候选复盘, 按生命线组织)
+ 范式 1 句式宪法 (主句=用户动作视角) + 范式 7 诚实水位 + 范式 8 合规固定段。

断言:
1. 故事线三态: 触价买入型 / 未触价延续型 / 过期作废型 (绝不静默消失)。
2. 退出归因卡: 推荐理由→持有天数→退出层级→盈亏 (stock_pool 退出段 + retrospect)。
3. 三段式收尾: 今日复盘 + 总体研判 + 明日方向 (必列候选延续计划)。
4. 持仓盯市改句式: 「你的持仓…距参考卖出线…」, 措辞 止损→参考卖出线。
5. 编排: 故事线置顶, 合规固定段在尾。

hermetic: 内存 sqlite (生命线四表 + stock_info), 不碰生产库。
"""
import sqlite3
from unittest.mock import MagicMock, patch

from report_layer.reports.base import ReportContext
from report_layer.reports.evening_report import EveningReportBuilder
from report_layer.sections.evening_close import EveningCloseSection
from report_layer.sections.evening_storyline import EveningStorylineSection
from report_layer.sections.exit_attribution import ExitAttributionSection
from report_layer.sections.market_one_liner import MarketOneLinerSection

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
        added_date="2026-08-26", created_at="2026-08-26 08:00:00",
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


def _ctx(con, date="2026-08-28"):
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = con
    data.db = db
    return ReportContext(date=date, data_layer=data)


class TestStorylineThreeStates:
    def test_bought_group_monitor_aggregated(self):
        # TASK_20260922_01 Phase B (批复裁决2): 持有组改三行式监控, 逐只
        # 「晨报推荐→触价→买入→持有」行退役; 断言同步翻新 — 覆盖行+一致行
        # 在, 未触发信号的持有票不逐只出现。
        con = _mkdb()
        _pool(con, "603309", "维力医疗", "active",
              added_date="2026-08-27", entry_low=17.71, entry_high=18.20,
              valid_until="2026-09-01", entry_price=17.35,
              entry_date="2026-08-28", stop_loss=16.90,
              last_price=17.93, unrealized_pnl_pct=3.3)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("603309", "维力医疗", "医疗器械"))
        con.execute(
            "INSERT INTO intraday_checks (code, check_type, current_price, "
            "trigger_price, status, detail, check_time) VALUES "
            "('603309','entry',17.35,NULL,'triggered','触价','2026-08-28 10:23:00')")
        r = EveningStorylineSection().build(_ctx(con))
        assert r is not None
        assert "全部在跟踪中" in r.body, "覆盖行: 持有监控机制"
        assert "持有中 1" in r.body
        assert "持续监控中" in r.body, "一致行: 无待办持续监控"
        assert "模拟买入" not in r.body, "逐只叙事行已退役, 不允许复活"

    def test_not_triggered_group_counted(self):
        # Phase B: 未触价组聚合一行计数; 仅余期 ≤2 天者逐只列名
        # (本夹具余 4 天 → 只计数不列名)。
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
              valid_until="2026-09-01", stop_loss=13.26)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("688349", "三一重能", "电气设备"))
        r = EveningStorylineSection().build(_ctx(con))
        assert "未触" in r.body
        assert "继续跟踪" in r.body
        assert "1 只候选" in r.body

    def test_expired_group_counted(self):
        # Phase B: 已过期组聚合一行计数, 计划作废仍是诚实水位 (不静默消失)。
        con = _mkdb()
        _pool(con, "000002", "万科A", "pending_entry",
              added_date="2026-08-20", entry_low=8.0, entry_high=8.5,
              valid_until="2026-08-27", stop_loss=7.5)  # 昨日过期, 未入场
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("000002", "万科A", "房地产"))
        r = EveningStorylineSection().build(_ctx(con, date="2026-08-28"))
        assert "作废" in r.body
        assert "1 只候选到期未触发" in r.body

    def test_empty_pool_honest(self):
        con = _mkdb()
        r = EveningStorylineSection().build(_ctx(con))
        assert r is not None
        assert "无候选" in r.body or "只看不动" in r.body


class TestExitAttribution:
    def test_exit_card_shows_attribution(self):
        con = _mkdb()
        _pool(con, "600001", "邯郸钢铁", "exited",
              added_date="2026-08-10", entry_low=5.0, entry_high=5.2,
              entry_price=5.1, entry_date="2026-08-11",
              exit_price=5.6, exit_date="2026-08-28",
              exit_layer="止盈", exit_reason="达到参考止盈位",
              total_return_pct=9.8, holding_days_actual=17,
              added_reason="低估值+资金流入")
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("600001", "邯郸钢铁", "钢铁"))
        r = ExitAttributionSection().build(_ctx(con))
        assert r is not None
        assert "邯郸钢铁" in r.body
        assert "止盈" in r.body
        assert "9.8" in r.body or "+9.8" in r.body

    def test_no_exits_honest(self):
        con = _mkdb()
        r = ExitAttributionSection().build(_ctx(con))
        assert r is not None
        assert "暂无" in r.body or "无退出" in r.body


class TestEveningClose:
    def test_close_has_three_parts_and_continuity(self):
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
              valid_until="2026-09-01", stop_loss=13.26)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("688349", "三一重能", "电气设备"))
        r = EveningCloseSection().build(_ctx(con))
        assert r is not None
        assert "今日复盘" in r.body
        assert "明日" in r.body
        assert "三一重能" in r.body  # 明日方向必列候选延续计划


class TestPositionDynamicsWording:
    def test_user_perspective_sentence(self):
        from report_layer.reports.evening_report import PositionDynamicsSection
        execution = MagicMock()
        snap = MagicMock()
        snap.positions = [{
            "code": "603309", "name": "维力医疗", "pnl_pct": 3.3,
            "stop_loss": 16.90, "last_price": 17.93, "score": 0.8,
            "holding_days": 1,
        }]
        execution.get_portfolio_snapshot.return_value = snap
        ctx = ReportContext(date="2026-08-28", execution_layer=execution)
        r = PositionDynamicsSection().build(ctx)
        assert r is not None
        assert "你的持仓" in r.body
        assert "参考卖出线" in r.body
        assert "止损" not in r.body  # 措辞表: 止损线→参考卖出线


class TestMarketOneLiner:
    def test_one_line_compressed(self):
        ctx = ReportContext(date="2026-08-28", data_layer=MagicMock())
        with patch("report_layer.sections.indices.fetch_realtime_indices",
                   return_value=[{"name": "上证指数", "price": 3200, "change_pct": 0.5}]):
            r = MarketOneLinerSection().build(ctx)
        assert r is not None
        assert "上证指数" in r.body


class TestEveningComposition:
    def test_storyline_first_compliance_last(self):
        # 报告宪法 v2 第一波 (2026-09-10): 昨日推荐点评置顶 (信誉位, 宪法 #2),
        # R3 段保留在编排骨干, 合规固定段仍置尾 — 断言同步翻新。
        names = [s.name for s in EveningReportBuilder().sections()]
        assert names[0] == "yesterday_picks_review"
        assert names[-1] == "risk_compliance"
        assert "evening_storyline" in names
        assert "exit_attribution" in names
        assert "evening_close" in names
