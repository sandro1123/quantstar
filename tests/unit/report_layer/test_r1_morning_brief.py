"""R1: 晨报行动指引化重编排 — 红绿测试。

依据: 报告体系重设计稿-C-20260828 §2.2 (七段结构) + D-277 行动卡七要素
+ 执行层定位校准-F-20260828 措辞表 (止损线→参考卖出线 等)
+ 无推荐日诚实模板双负对照 (空池 / 数据缺口)。

hermetic: 内存 sqlite (生命线四表 + stock_info + factor_daily), 不碰生产库。
"""
import json
import sqlite3
from unittest.mock import patch

from report_layer.lifeline import build_lifelines
from report_layer.reports.base import ReportContext
from report_layer.reports.morning_brief import MorningBriefBuilder
from report_layer.sections.action_candidates import ActionCandidatesSection
from report_layer.sections.avoid_cards import AvoidCardsSection
from report_layer.sections.market_background import MarketBackgroundSection
from report_layer.sections.risk_compliance import RiskComplianceSection
from report_layer.sections.yesterday_followup import YesterdayFollowupSection

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
        CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT);
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
        entry_plan_source=None)
    defaults.update(kw)
    cols = ", ".join(defaults)
    ph = ", ".join("?" * (len(defaults) + 3))
    con.execute(
        f"INSERT INTO stock_pool (code, name, status, {cols}) VALUES ({ph})",
        (code, name, status, *defaults.values()))


def _seed_candidate(con):
    """一只要素齐全的 pending_entry 候选 (生产 688349 形态)。"""
    _pool(con, "688349", "三一重能", "pending_entry",
          added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
          valid_until="2026-09-01", stop_loss=13.26, take_profit=15.0,
          initial_score=0.37)
    con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                ("688349", "三一重能", "电气设备"))
    con.execute("INSERT INTO factor_daily VALUES (?,?,?)",
                ("2026-08-27", "688349", json.dumps(
                    {"value_discount": 0.65, "momentum_20d": -1.35,
                     "roe_quality": 0.44})))


class _FakeDB:
    def __init__(self, con):
        self._con = con

    def get_connection(self):
        return self._con


class _FakeDL:
    def __init__(self, con):
        self.db = _FakeDB(con)


def _ctx(con=None, date="2026-08-28"):
    return ReportContext(date=date,
                         data_layer=_FakeDL(con) if con is not None else None)


class TestR1BuilderComposition:
    """设计稿 §2.2: 七段新编排, 指引段前置背景段后置, 日志段移出。"""

    def test_seven_sections_in_order(self):
        names = [s.name for s in MorningBriefBuilder().sections()]
        assert names == [
            "outbox_watermark",      # 基建置顶 (无滞留自动隐身)
            "action_candidates",     # 🎯 今日行动候选 (行动卡组)
            "avoid_cards",           # 🔴 回避卡
            "advisory_morning",      # 🧭 晨间投顾 (卡VII, 隔夜+昨收视角)
            "yesterday_followup",    # 🧵 昨日候选, 现在如何
            "market_background",     # 📊 市场背景 (压缩段)
            "risk_compliance",       # ⚠️ 风险提示 + 合规固定段
            "conclusion",            # 结论
        ]

    def test_log_sections_removed_from_morning(self):
        names = {s.name for s in MorningBriefBuilder().sections()}
        # D2: ICWeight 移出; MarketOverview 并入背景; D5: AgentTradePlan 移出;
        # watch_focus/stock_picks 由行动卡组取代 (旧模块保留, 仅移出晨报)
        for gone in ("ic_weight", "market_overview", "agent_trade_plan",
                     "watch_focus", "stock_picks", "overnight_market",
                     "news", "indices"):
            assert gone not in names


class TestActionCandidates:
    """行动卡七要素: 标的/方向/入场区间/参考卖出线/止盈/有效期/一句理由。"""

    def test_full_card_seven_elements(self):
        con = _mkdb()
        _seed_candidate(con)
        result = ActionCandidatesSection().build(_ctx(con))
        assert result is not None
        body = result.body
        # 标题行: 绿=可买 + 名称(code) + 行业
        assert "🟢 三一重能(688349) · 电气设备" in body
        # 行动行: 区间 + 参考卖出线(约 -x%) + 止盈 + 有效期(余 N 天)
        # morning-topn-converge 裁定#4 (P6 定调): 「可以买」→「进入模拟买入区间」
        assert "进入模拟买入区间: 13.79–14.15" in body
        assert "可以买" not in body
        assert "参考卖出线: 13.26" in body
        assert "约 -6.3%" in body
        assert "参考止盈: 15.00" in body
        assert "有效至 2026-09-01" in body
        assert "余 4 天" in body
        # 理由行: 因子中文化 (复用 _factor_cn), 禁因子英文缩写
        assert "估值折价" in body
        assert "动量(20日)" in body
        assert "value_discount" not in body
        # 署名行: morning-topn-converge 裁定#6 — 审批中文+置信, 来源名不裸漏
        # (无 human_review_queue → 未送审; 内部生命线 ID 不进卡片正文)
        assert "审批 未送审" in body
        assert "置信 37%" in body
        assert "来源" not in body
        assert "688349@2026-08-27" not in body

    def test_wording_table_no_trading_terms(self):
        # 措辞表 (F-20260828): 止损线 → 参考卖出线; 用户视角非账户视角
        con = _mkdb()
        _seed_candidate(con)
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "止损" not in body
        assert "参考卖出线" in body

    def test_watch_candidate_blue_icon(self):
        # 观察池入场就绪 (score>0.6) → 蓝=观望信息色
        con = _mkdb()
        _pool(con, "300360", "炬华科技", "watch",
              added_date="2026-08-25", entry_low=11.39, entry_high=11.65,
              valid_until="2026-09-02", stop_loss=11.0, initial_score=0.7)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("300360", "炬华科技", "电器仪表"))
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "🔵 炬华科技(300360)" in body

    def test_reason_line_covers_production_factor_keys(self):
        # 生产 factor_scores 实测键 (Qlib alpha158 族) 必须中文化, 禁英文泄漏
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
              valid_until="2026-09-01", stop_loss=13.26, initial_score=0.37)
        con.execute("INSERT INTO factor_daily VALUES (?,?,?)",
                    ("2026-08-27", "688349", json.dumps(
                        {"klow2": 2.30, "sumd20": -2.20, "roc5": 1.10})))
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "下影线" in body
        assert "20日累计跌幅" in body
        assert "5日涨速" in body
        assert "klow2" not in body and "sumd20" not in body

    def test_reason_line_skips_unknown_factor_keys(self):
        # 未知键不泄漏英文: 理由行整体省略而非显示原始键名
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
              valid_until="2026-09-01", stop_loss=13.26, initial_score=0.37)
        con.execute("INSERT INTO factor_daily VALUES (?,?,?)",
                    ("2026-08-27", "688349", json.dumps(
                        {"zzz_unknown_factor": 2.30})))
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "zzz_unknown_factor" not in body
        assert "理由：" not in body

    def test_expired_candidate_excluded(self):
        con = _mkdb()
        _pool(con, "600001", "过期甲", "pending_entry",
              entry_low=10.0, entry_high=10.5, valid_until="2026-08-27")
        result = ActionCandidatesSection().build(_ctx(con))  # date=08-28
        assert "过期甲" not in result.body
        assert "全部候选已过期" in result.body

    def test_st_candidate_goes_to_avoid_not_action(self):
        con = _mkdb()
        _pool(con, "600002", "*ST测试", "pending_entry",
              entry_low=5.0, entry_high=5.5, valid_until="2026-09-01")
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "*ST测试" not in body

    def test_honest_template_empty_pool(self):
        # 负对照 ①: 空池 → 诚实模板 (范式 7)
        con = _mkdb()
        result = ActionCandidatesSection().build(_ctx(con))
        assert result is not None
        assert "⚪ 今日无操作建议" in result.body
        assert "只看不动" in result.body

    def test_honest_template_data_gap(self):
        # 负对照 ②: 数据缺口 (data_layer 缺失) → 诚实模板不崩溃
        result = ActionCandidatesSection().build(_ctx(None))
        assert result is not None
        assert "⚪ 今日无操作建议" in result.body
        assert "数据缺口" in result.body
        assert "只看不动" in result.body


class TestAvoidCards:
    def test_st_stock_red_card(self):
        con = _mkdb()
        _pool(con, "600002", "*ST测试", "pending_entry",
              entry_low=5.0, entry_high=5.5, valid_until="2026-09-01")
        result = AvoidCardsSection().build(_ctx(con))
        assert result is not None
        assert "🔴" in result.body
        assert "*ST测试" in result.body
        assert "不要买" in result.body

    def test_no_avoid_target_hides_section(self):
        con = _mkdb()
        _seed_candidate(con)
        assert AvoidCardsSection().build(_ctx(con)) is None

    def test_no_db_hides_section(self):
        assert AvoidCardsSection().build(_ctx(None)) is None


class TestYesterdayFollowup:
    """连续性段: 昨日每只候选一句话结局 (触价买入/继续/过期作废)。"""

    def test_three_outcomes(self):
        con = _mkdb()
        # A: 昨日触价买入
        _pool(con, "600001", "测试甲", "active",
              added_date="2026-08-26", entry_low=10.0, entry_high=10.5,
              valid_until="2026-09-01", entry_price=10.2,
              entry_date="2026-08-27", stop_loss=9.5)
        # B: 未触价, 计划继续
        _pool(con, "600002", "测试乙", "pending_entry",
              added_date="2026-08-26", entry_low=20.0, entry_high=21.0,
              valid_until="2026-09-01", stop_loss=19.0)
        # C: 昨日到期作废
        _pool(con, "600003", "测试丙", "pending_entry",
              added_date="2026-08-25", entry_low=30.0, entry_high=31.0,
              valid_until="2026-08-27", stop_loss=28.0)
        result = YesterdayFollowupSection().build(_ctx(con))
        assert result is not None
        body = result.body
        assert "测试甲" in body and "已触价买入" in body
        assert "测试乙" in body and "计划继续" in body
        assert "测试丙" in body and "作废" in body

    def test_no_yesterday_candidates_hides(self):
        con = _mkdb()
        assert YesterdayFollowupSection().build(_ctx(con)) is None

    def test_no_db_hides(self):
        assert YesterdayFollowupSection().build(_ctx(None)) is None


class TestMarketBackground:
    """压缩段: 外盘+指数+要闻合并一段。"""

    def test_merges_three_sources(self):
        from report_layer.sections.news import NewsSection
        from report_layer.sections.overnight_market import OvernightMarketSection
        ctx = _ctx(None)
        with patch.object(OvernightMarketSection, "_fetch_overnight",
                          return_value={"dow": 45000.0, "dow_pct": 0.5}), \
             patch("report_layer.sections.indices.fetch_realtime_indices",
                   return_value=[{"name": "上证", "price": 3250.0,
                                  "change_pct": 0.5}]), \
             patch.object(NewsSection, "_get_items",
                          return_value=[{"title": "央行降准",
                                         "summary": "释放流动性",
                                         "sentiment": "bullish"}]):
            result = MarketBackgroundSection().build(ctx)
        assert result is not None
        assert "道琼斯" in result.body
        assert "上证" in result.body
        assert "央行降准" in result.body

    def test_all_unavailable_graceful(self):
        from report_layer.sections.news import NewsSection
        from report_layer.sections.overnight_market import OvernightMarketSection
        ctx = _ctx(None)
        with patch.object(OvernightMarketSection, "_fetch_overnight",
                          return_value={}), \
             patch("report_layer.sections.indices.fetch_realtime_indices",
                   return_value=[]), \
             patch.object(NewsSection, "_get_items", return_value=[]):
            result = MarketBackgroundSection().build(ctx)
        assert result is not None
        assert "暂不可用" in result.body


class TestRiskCompliance:
    def test_compliance_line_always_present(self):
        # 范式 8: 「模拟盘研究性质, 非投资建议」每报必带
        result = RiskComplianceSection().build(_ctx(None))
        assert result is not None
        assert "模拟盘研究" in result.body
        assert "非投资建议" in result.body

    def test_wraps_risk_tips_body(self):
        from data_layer._models import DataSnapshot, Kline

        class _DL:
            def snapshot(self, date):
                return DataSnapshot(
                    date=date,
                    klines=[Kline(code="000001", date=date, open=10, high=11,
                                  low=9, close=9.5, volume=1000, amount=10000,
                                  change_pct=-4.0)],
                    market=None, financials=[], valuations=[], moneyflow=[],
                    news=[], limit_up_down=[], readiness=None)

        result = RiskComplianceSection().build(_ctx(None).__class__(
            date="2026-08-28", data_layer=_DL()))
        assert "跌幅超 3%" in result.body
        assert "非投资建议" in result.body


class TestLifelineEnrichment:
    """R1 扩展: stock_pool.name 为 code 时用 stock_info 补名称/行业。"""

    def test_name_industry_enriched_from_stock_info(self):
        con = _mkdb()
        _pool(con, "603309", "603309", "active",
              entry_low=11.25, entry_high=11.53, stop_loss=10.81)
        con.execute("INSERT INTO stock_info VALUES (?,?,?)",
                    ("603309", "维力医疗", "医疗保健"))
        lls = build_lifelines(con)
        assert lls[0].name == "维力医疗"
        assert lls[0].industry == "医疗保健"
        assert lls[0].source == "strategy"

    def test_missing_stock_info_table_no_crash(self):
        con = sqlite3.connect(":memory:")
        con.executescript("""
            CREATE TABLE stock_pool (
                code TEXT, name TEXT, status TEXT, added_date TEXT,
                created_at TEXT, entry_plan_date TEXT, entry_low REAL,
                entry_high REAL, valid_until TEXT, entry_price REAL,
                entry_date TEXT, stop_loss REAL, trailing_stop REAL,
                dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
                exit_price REAL, exit_date TEXT, net_return_pct REAL,
                review_quality TEXT);
            CREATE TABLE action_records (
                id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
                price REAL, reason TEXT, created_at TEXT);
            CREATE TABLE intraday_checks (
                id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
                current_price REAL, trigger_price REAL, status TEXT,
                detail TEXT, check_time TEXT);
            CREATE TABLE recommendation_retrospect (
                prediction_id INTEGER, code TEXT, rec_date TEXT,
                verdict TEXT, hit_5d INTEGER, hit_10d INTEGER,
                fwd_return_5d REAL, fwd_return_10d REAL);
        """)
        con.execute(
            "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("600001", "测试甲", "watch", "2026-08-26",
             "2026-08-26 08:00:00", None, None, None, None, None, None,
             None, None, None, None, None, None, None, None, None))
        lls = build_lifelines(con)
        assert lls[0].name == "测试甲"
        assert lls[0].industry is None
