# -*- coding: utf-8 -*-
"""报告层第一波 TDD RED 底稿 — 晚报旗舰六段 + 盘前速报 + 推送时序。

依据: REPORT_CONSTITUTION.md v2 (14 条) + 统筹派单 sm-6ed8eb24。
hermetic: 内存 sqlite, 不碰生产库; 断言按宪法逐条锚。

RED 语义 (施工前必须红):
- R1 晚报六段编排: YesterdayPicksReview(置顶信誉位) / MarketWrap / SectorNews /
  TomorrowPicks(四行结构+N=clamp(3,8)) / PositionSteward(持仓+跟踪池一表) /
  PreviewHealth(明日预演+系统健康3行)。
- R2 昨日推荐点评三态: 触发买入→浮盈%; 未触发→等/废; 逐票一行+后续操作建议;
  EX-3 快照缺数据日诚实「快照积累中」。
- R3 明日推荐四行结构: 为什么选它(四因归因自然语句)/在哪买(价区)/
  什么情况买(条件)/何时卖(目标+止损); N clamp(质量分布, 3, 8)。
- R4 premarket_brief: 新 builder content_type="premarket_brief" + 竞价快评
  (auction_snapshot 数据; 缺竞价数据诚实「竞价数据积累中」)。
- R5 push_rules: premarket_brief production 路由 [feishu_group, feishu_dm]。
- R6 ops.yaml 调度: evening_report schedule "20:00" + premarket_brief "09:25"
  (代码内调度配置面, crontab 行窗内统筹亲执)。
"""
import sqlite3

import yaml

import pytest

from report_layer.reports.base import ReportContext
from report_layer.reports.evening_report import EveningReportBuilder

POOL_COLS = """
    code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
    entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
    entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
    dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
    exit_price REAL, exit_date TEXT, net_return_pct REAL,
    source TEXT, initial_score REAL,
    last_price REAL, unrealized_pnl_pct REAL, holding_days INTEGER,
    entry_reason TEXT, added_reason TEXT
"""
KLINE_COLS = "code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL"


def _mkdb() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(f"""
        CREATE TABLE stock_pool ({POOL_COLS});
        CREATE TABLE daily_klines ({KLINE_COLS});
        CREATE TABLE auction_snapshot (
            date TEXT, code TEXT, name TEXT, auction_price REAL,
            auction_pct REAL, auction_volume REAL, auction_amount REAL,
            auction_unmatched REAL, auction_turnover_pct REAL, source TEXT,
            created_at TEXT);
        CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT);
        CREATE TABLE stock_news (
            id INTEGER PRIMARY KEY, code TEXT, title TEXT, content TEXT,
            publish_time TEXT, source TEXT);
    """)
    return con


def _ctx(con, date="2026-09-10"):
    dl = type("DL", (), {})()
    db = type("DB", (), {"get_connection": staticmethod(lambda: con)})()
    dl.db = db
    return ReportContext(date=date, data_layer=dl)


class TestR1EveningSixSections:
    def test_builder_sections_order_and_names(self):
        b = EveningReportBuilder()
        names = [s.name for s in b.sections()]
        # 可读性施工 (2026-09-20): 昨日推荐点评仍信誉位打头 (宪法 #2),
        # 行动类段 (明日推荐) 紧随其后尽量靠前; 大盘总结在持仓管家之后。
        # TASK_20260922_01 Phase B (批复裁决2): 持仓管家段退役, 持有组三行式
        # 并入故事线 — 前三 = 点评/明日推荐/大盘, 断言同步翻新。
        assert names[:3] == ["yesterday_picks_review", "tomorrow_picks",
                             "market_wrap"], (
            "宪法 #2: 昨日推荐点评必须置顶 (信誉位打头); 行动类段尽量靠前")

    def test_all_six_present(self):
        b = EveningReportBuilder()
        names = [s.name for s in b.sections()]
        for want in ("yesterday_picks_review", "market_wrap", "sector_news",
                     "tomorrow_picks", "evening_storyline", "preview_health"):
            assert want in names, f"六段缺 {want}"


class TestR2YesterdayPicksReview:
    def test_three_states_line_each(self):
        from report_layer.sections.yesterday_picks_review import (
            YesterdayPicksReviewSection,
        )
        con = _mkdb()
        # 昨日推荐三只: 已触发(active 有 entry_date/entry_price), 未触发(watch), 过期(exited)
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,entry_date,"
            "entry_price,last_price,unrealized_pnl_pct,valid_until) VALUES "
            "('000001','平安银行','active','2026-09-09','2026-09-09',"
            "10.0,10.5,5.0,'2026-09-13')")
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,valid_until,"
            "entry_low,entry_high) VALUES "
            "('600000','浦发银行','watch','2026-09-09','2026-09-13',8.0,8.5)")
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,valid_until)"
            " VALUES ('999999','过期股','exited','2026-09-09','2026-09-09')")
        sec = YesterdayPicksReviewSection()
        res = sec.build(_ctx(con))
        assert res is not None, "有昨日候选必须有段 (绝不静默消失)"
        assert "000001" in res.body and "600000" in res.body
        assert "+5.0%" in res.body, "触发买入的必须报浮盈%"

    def test_snapshot_accumulating_honest_fallback(self):
        from report_layer.sections.yesterday_picks_review import (
            YesterdayPicksReviewSection,
        )
        con = _mkdb()
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,entry_date,"
            "entry_price,unrealized_pnl_pct,valid_until) VALUES "
            "('000001','平安银行','active','2026-09-09','2026-09-09',"
            "10.0,NULL,'2026-09-13')")
        sec = YesterdayPicksReviewSection()
        res = sec.build(_ctx(con))
        assert res is not None
        assert "快照积累中" in res.body, "EX-3 快照缺数据日诚实降级, 不编数"


class TestR3TomorrowPicks:
    def test_top3_contract_replaces_clamp(self):
        # TASK_20260922_01 Phase B (批复裁决1): Top N=3 固定 (composite 降序,
        # 宁缺毋滥), N=clamp(3,8) 语义退役 — 断言同步翻新。
        from report_layer.sections import tomorrow_picks
        assert tomorrow_picks.TOP_N == 3
        assert not hasattr(tomorrow_picks.TomorrowPicksSection, "clamp_n")

    def test_missing_data_honest(self):
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection
        con = _mkdb()
        sec = TomorrowPicksSection()
        ctx = _ctx(con)
        ctx.strategy_layer = None
        res = sec.build(ctx)
        assert res is not None
        assert "暂无" in res.body or "积累中" in res.body


class TestR4PremarketBrief:
    def test_builder_exists_with_content_type(self):
        from report_layer.reports.premarket_brief import PremarketBriefBuilder
        b = PremarketBriefBuilder()
        assert b.content_type == "premarket_brief"

    def test_auction_quick_review(self):
        from report_layer.sections.premarket_review import PremarketReviewSection
        con = _mkdb()
        con.execute(
            "INSERT INTO auction_snapshot (date,code,name,auction_price,"
            "auction_pct,auction_volume,auction_turnover_pct,source,created_at)"
            " VALUES ('2026-09-10','000007','全新好',5.2,4.2,880000.0,2.8,"
            "'fuyao','2026-09-10 09:25:00')")
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,valid_until)"
            " VALUES ('000007','全新好','watch','2026-09-09','2026-09-13')")
        sec = PremarketReviewSection()
        res = sec.build(_ctx(con, "2026-09-10"))
        assert res is not None
        assert "000007" in res.body and "4.2%" in res.body, (
            "竞价快评必须带竞价涨幅与操作指引")

    def test_auction_missing_honest(self):
        from report_layer.sections.premarket_review import PremarketReviewSection
        con = _mkdb()
        con.execute(
            "INSERT INTO stock_pool (code,name,status,added_date,valid_until)"
            " VALUES ('000008','赛格','watch','2026-09-09','2026-09-13')")
        sec = PremarketReviewSection()
        res = sec.build(_ctx(con, "2026-09-10"))
        assert res is not None
        assert "竞价数据积累中" in res.body


class TestR5PushRules:
    def test_premarket_routed_to_feishu_group(self):
        with open("config/defaults/delivery.yaml", encoding="utf-8") as f:
            rules = yaml.safe_load(f)["delivery"]["push_rules"]
        # 第四波 (09-11): 技术报告群主通道单端 (三端语义 v2)
        assert rules.get("premarket_brief", {}).get("production") == [
            "feishu_group"], "盘前速报须入推送路由 (宪法 #7, 群主通道)"


class TestR6Schedule:
    def test_evening_report_at_2000(self):
        with open("config/defaults/ops.yaml", encoding="utf-8") as f:
            ops = yaml.safe_load(f)
        assert ops["ops"]["pipelines"]["evening_report"]["schedule"] == "20:00", (
            "宪法 #3: 晚报时间=交易日 20:00")

    def test_premarket_brief_at_0925(self):
        with open("config/defaults/ops.yaml", encoding="utf-8") as f:
            ops = yaml.safe_load(f)
        assert ops["ops"]["pipelines"]["premarket_brief"]["schedule"] == "09:25", (
            "宪法 #7: 盘前速报 09:25 (集合竞价阶段)")
