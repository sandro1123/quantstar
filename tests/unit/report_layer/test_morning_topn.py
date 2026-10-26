# -*- coding: utf-8 -*-
"""晨报 Top N 收敛 (TASK: morning-topn-converge · 复用 Phase B 闸件) — 红绿测试。

依据统筹派单裁定:
  #1 action_candidates Top 3 (initial_score 降序+code 决胜, 宁缺毋滥),
     第 4 只起降一行折叠「另有 N 只候选，全量见深钻页 P0」;
  #2 理由行: advisor_text 优先 (_pick_reason 冻结单点), 缺省保留因子中文化行
     (真理由, 不用「生成中」占位替换);
  #4 「可以买」→「进入模拟买入区间」(P6 定调中性);
  #5 avoid_cards / yesterday_followup 各限 Top 3, 超出降一行计数;
  #6 署名行「来源: X」→「审批 {中文} · 置信 N%」(内部口径不裸漏);
  #7 晨报 content["date"] 注入 → 卡片标题带日期 (基类已塞, 本钉守住不再退化)。

hermetic: 内存 sqlite (与 test_r1_morning_brief 同构), 不碰生产库, 不走全链。
"""
import json
import sqlite3
import types
from unittest.mock import MagicMock

from report_layer.formatters.feishu_card import FeishuCardFormatter
from report_layer.reports.base import ReportContext
from report_layer.reports.morning_brief import MorningBriefBuilder
from report_layer.sections.action_candidates import (
    ActionCandidatesSection, _card)
from report_layer.sections.avoid_cards import AvoidCardsSection
from report_layer.sections.yesterday_followup import YesterdayFollowupSection
from report_reason import RANK_PHRASE_RE

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


def _ctx(con=None, date="2026-08-28"):
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = con
    data.db = db
    return ReportContext(date=date, data_layer=data if con is not None else None)


def _seed_candidates(con, n=5, date="2026-08-28"):
    """n 只 pending_entry 候选 (有效期内, 分数递降 0.9→0.7, 全 >0.6 就绪)。"""
    for i in range(n):
        _pool(
            con, f"30000{i}", f"晨选{i}", "pending_entry",
            added_date="2026-08-27", entry_low=10.0 + i, entry_high=10.5 + i,
            stop_loss=9.5 + i, take_profit=11.5 + i,
            valid_until="2026-09-02", initial_score=0.9 - i * 0.05)


class TestMorningTopN:
    def test_morning_candidates_max_3(self):
        """裁定#1: 候选 >3 只时只出 Top3 (宁缺毋滥)。"""
        con = _mkdb()
        _seed_candidates(con, 5)
        r = ActionCandidatesSection().build(_ctx(con))
        body = r.body
        assert body.count("🟢") == 3, body
        assert "(3 只)" in body.splitlines()[0]
        assert not RANK_PHRASE_RE.search(body)

    def test_morning_collapse_line_counts_rest(self):
        """裁定#1: 第 4 只起一行折叠计数指深钻页 P0, 代码不进正文。"""
        con = _mkdb()
        _seed_candidates(con, 5)
        r = ActionCandidatesSection().build(_ctx(con))
        body = r.body
        assert "300003" not in body and "300004" not in body
        assert "另有 2 只候选" in body
        assert "深钻页 P0" in body

    def test_morning_no_buy_imperative(self):
        """裁定#4 (P6 定调): 「可以买」→「进入模拟买入区间」, 祈使句绝迹。"""
        con = _mkdb()
        _seed_candidates(con, 2)
        body = ActionCandidatesSection().build(_ctx(con)).body
        # 「回避卡」的「不要买」保留; 买入位不许出现「可以买」
        assert "可以买" not in body
        assert "进入模拟买入区间" in body

    def test_morning_reason_keeps_factor_line(self):
        """裁定#2: 因子中文化行是真理由, 不被「生成中」占位替换。"""
        con = _mkdb()
        _pool(con, "688349", "三一重能", "pending_entry",
              added_date="2026-08-27", entry_low=13.79, entry_high=14.15,
              stop_loss=13.26, valid_until="2026-09-01", initial_score=0.8)
        con.execute("INSERT INTO factor_daily VALUES (?,?,?)",
                    ("2026-08-27", "688349", json.dumps(
                        {"value_discount": 0.65, "roc20": 0.44})))
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "理由：" in body
        assert "估值折价" in body and "20日涨速" in body
        # 关键负断言: 有因子行时不被「生成中」占位替换 (裁定#2 原义)
        assert "生成中" not in body

    def test_morning_advisor_preferred_when_present(self):
        """裁定#2: advisor_text 存在即用 (冻结单点 _pick_reason), 接口先对。"""
        con = _mkdb()
        ll = types.SimpleNamespace(
            status="pending_entry", name="晨选0", code="300000",
            industry="电气设备", entry_low=10.0, entry_high=10.5,
            stop_loss=9.5, take_profit=11.5, valid_until="2026-09-02",
            initial_score=0.9, advisor_text="顾问真理由：基本面拐点已现",
            entry_plan_source=None, source="strategy")
        lines = _card(ll, con, "2026-08-28")
        text = "\n".join(lines)
        assert "顾问真理由：基本面拐点已现" in text
        assert "生成中" not in text

    def test_no_source_leak_in_attribution(self):
        """裁定#6: 署名行 = 审批中文 + 置信, 内部来源名 (strategy/source) 不裸漏。"""
        con = _mkdb()
        _seed_candidates(con, 1)
        body = ActionCandidatesSection().build(_ctx(con)).body
        assert "置信" in body
        assert "审批" in body
        assert "来源" not in body
        assert "strategy" not in body

    def test_morning_title_has_date(self):
        """裁定#7: 晨报卡片标题带数据日期 (content["date"] 注入, 守住不再退化)。"""
        con = _mkdb()
        b = MorningBriefBuilder()
        # 只留行动段构建, 其余段依赖外部源 (本钉只验 date 注入链路)
        b.sections = lambda: [ActionCandidatesSection()]
        msg = b.build(_ctx(con))
        assert msg is not None
        assert msg.content.get("date") == "2026-08-28"
        card = FeishuCardFormatter().format(msg)
        assert "2026-08-28" in card["card"]["header"]["title"]["content"]


class TestAvoidAndFollowupCollapse:
    def test_avoid_followup_collapsed(self):
        """裁定#5: avoid / followup 各限 Top 3, 超出降一行计数指深钻页。"""
        con = _mkdb()
        for i in range(4):  # 4 只 ST 回避对象
            _pool(con, f"60000{i}", f"*ST避让{i}", "pending_entry",
                  entry_low=5.0, entry_high=5.5, valid_until="2026-09-01",
                  initial_score=0.3)
        body = AvoidCardsSection().build(_ctx(con)).body
        assert "(4 只)" in body.splitlines()[0]  # 头行总数不缩水
        assert body.count("不要买") == 3
        assert "*ST避让3" not in body
        assert "另有 1 只" in body and "深钻页 P0" in body

    def test_avoid_three_or_fewer_listed_fully(self):
        con = _mkdb()
        for i in range(3):
            _pool(con, f"60000{i}", f"*ST避让{i}", "pending_entry",
                  entry_low=5.0, entry_high=5.5, valid_until="2026-09-01")
        body = AvoidCardsSection().build(_ctx(con)).body
        assert body.count("不要买") == 3
        assert "另有" not in body

    def test_followup_collapsed_beyond_3(self):
        con = _mkdb()
        # 1 只昨日触价 + 3 只计划继续 + 1 只作废 = 5 条结局
        _pool(con, "600001", "触价甲", "active",
              added_date="2026-08-26", entry_low=10.0, entry_high=10.5,
              valid_until="2026-09-01", entry_price=10.2,
              entry_date="2026-08-27", stop_loss=9.5)
        for i in range(3):
            _pool(con, f"6001{i}0", f"继续{i}", "pending_entry",
                  added_date="2026-08-26", entry_low=20.0, entry_high=21.0,
                  valid_until="2026-09-01", stop_loss=19.0)
        _pool(con, "600130", "作废丁", "pending_entry",
              added_date="2026-08-25", entry_low=30.0, entry_high=31.0,
              valid_until="2026-08-27", stop_loss=28.0)
        r = YesterdayFollowupSection().build(_ctx(con))
        body = r.body
        # 结局总数在头行; 逐只行只 3 条, 触价者优先
        assert "(5 只)" in body.splitlines()[0]
        assert "触价甲" in body and "已触价买入" in body
        listed = [ln for ln in body.splitlines()
                  if ln.startswith(("✅", "⏳", "⚪"))]
        assert len(listed) == 3
        assert "另有 2 只" in body and "深钻页 P0" in body

    def test_followup_three_or_fewer_listed_fully(self):
        con = _mkdb()
        for i in range(3):
            _pool(con, f"6001{i}0", f"继续{i}", "pending_entry",
                  added_date="2026-08-26", entry_low=20.0, entry_high=21.0,
                  valid_until="2026-09-01", stop_loss=19.0)
        body = YesterdayFollowupSection().build(_ctx(con)).body
        assert "另有" not in body
        assert body.count("⏳") == 3
