# -*- coding: utf-8 -*-
"""TASK_20260922_01 Phase B 结构钉 (先红后绿, 渲染测试直接构造 dict, 不走全链)。

依据: ANSWER_TASK_20260922_01_PhaseA批复 (四点裁决, 施工法定依据)
  裁决1: Top N=3 批 (composite 降序, 宁缺毋滥不写满); 其余折叠计数。
  裁决2: 持有组三行式监控 (覆盖行/待行动行/一致行), 永不退化成逐只罗列;
         跟踪明细表 (recommendation_tracking/advice) 未落地 → 回退 stock_pool
         派生并诚实标注; 无数据支撑的「理由仍然成立」不许写。
  裁决3: 理由闸 — 排名模板 (候选池第 N 位/综合得分 x.xxx) 永不进卡,
         显式「量化评分入选，个股依据生成中」; advisor_text 存在即用。
  裁决4 + 展示方向: DM/晚报走同一 _pick_reason 单点, 口径统一。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from report_layer.reports.evening_report import ActionSummarySection
from report_layer.reports.base import ReportContext
from report_layer.sections.evening_storyline import EveningStorylineSection
from report_layer.sections.tomorrow_picks import TomorrowPicksSection
from report_reason import GENERATING_TEXT, RANK_PHRASE_RE, _pick_reason

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
    """)
    return con


def _ctx(con, date="2026-09-21", with_strategy=True) -> ReportContext:
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = con
    data.db = db
    ctx = ReportContext(date=date, data_layer=data)
    if with_strategy:
        ctx.strategy_layer = MagicMock()
    return ctx


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


RANK_REASON = "候选池第 1/31 位, 综合得分 0.917"


def _seed_candidates(con, n=5, date="2026-09-21"):
    """n 只候选 (pending_entry, 有效期内, 分数递降, 理由=排名模板)。"""
    for i in range(n):
        code = f"10000{i}"
        _pool(
            con, code, f"候选股{i}", "pending_entry",
            entry_low=10.0 + i, entry_high=10.5 + i, stop_loss=9.5 + i,
            take_profit=11.5 + i, valid_until="2026-09-30",
            initial_score=0.9 - i * 0.05,
            added_reason=f"候选池第 {i + 1}/{n} 位, 综合得分 {0.9 - i * 0.01:.3f}")


class TestTopPicksStructure:
    def test_top_picks_max_3(self):
        """裁决1: 候选 >3 只时只出 Top3 (宁缺毋滥, 不写满)。"""
        con = _mkdb()
        _seed_candidates(con, 5)
        r = TomorrowPicksSection().build(_ctx(con))
        assert r is not None
        assert r.body.count("◆") == 3
        assert "明日推荐（3 只）" in r.body.splitlines()[0]

    def test_each_pick_has_five_piece(self):
        """铁原则3: 每只 Top 五件套齐 — 为什么/在哪买/什么情况买/何时卖/信心与折扣。"""
        con = _mkdb()
        _seed_candidates(con, 3)
        r = TomorrowPicksSection().build(_ctx(con))
        body = r.body
        for piece in ("为什么选它", "在哪买", "什么情况买", "何时卖", "信心与折扣"):
            assert body.count(piece) == 3, f"五件套缺件: {piece}"
        assert "综合分" in body, "信心与折扣件必须含综合分"
        assert "审批" in body, "信心与折扣件必须含审批状态"

    def test_pool_collapsed_not_in_body(self):
        """铁原则1: 第 4 只起代码不进卡片正文, 一行折叠计数指深钻页 P0。"""
        con = _mkdb()
        _seed_candidates(con, 5)
        r = TomorrowPicksSection().build(_ctx(con))
        assert "100003" not in r.body and "100004" not in r.body
        assert "另有 2 只候选" in r.body
        assert "综合分" in r.body and "深钻页 P0" in r.body


class TestReasonGuard:
    def test_no_rank_phrase_as_reason(self):
        """裁决3: 排名模板永不进卡 — why 行是「生成中」不是排名话术。"""
        con = _mkdb()
        _seed_candidates(con, 3)
        r = TomorrowPicksSection().build(_ctx(con))
        assert not RANK_PHRASE_RE.search(r.body)
        assert GENERATING_TEXT in r.body

    def test_real_reason_passes_through(self):
        con = _mkdb()
        _pool(con, "100000", "候选股0", "pending_entry",
              entry_low=10.0, entry_high=10.5, valid_until="2026-09-30",
              initial_score=0.9, added_reason="低估值+北向资金连续净流入")
        r = TomorrowPicksSection().build(_ctx(con))
        assert "低估值+北向资金连续净流入" in r.body

    def test_advisor_text_preferred_when_present(self):
        """接口预留: advisor_text/recommendation_rationale 存在即用 (零改动升级)。"""
        row = {"advisor_text": "真理由：基本面拐点已现",
               "added_reason": RANK_REASON}
        assert _pick_reason(row) == "真理由：基本面拐点已现"
        row2 = {"recommendation_rationale": "一阶理由", "added_reason": RANK_REASON}
        assert _pick_reason(row2) == "一阶理由"

    def test_pick_reason_never_returns_rank_phrase(self):
        assert _pick_reason({"added_reason": RANK_REASON}) == GENERATING_TEXT
        assert _pick_reason({"added_reason": "综合评分排名第 3"}) == GENERATING_TEXT
        assert _pick_reason({"added_reason": ""}) == "个股依据生成中"
        assert _pick_reason({"added_reason": None}) == "个股依据生成中"


class TestHoldingsThreeLine:
    def _seed_holdings(self, con):
        # 3 只持有: 一只距卖出线 1.6% (触发待行动), 一只 8%, 一只贴近目标位
        _pool(con, "600001", "持有股A", "active",
              added_date="2026-09-10", entry_price=10.0, entry_date="2026-09-11",
              stop_loss=9.5, last_price=9.65, unrealized_pnl_pct=-3.5)
        _pool(con, "600002", "持有股B", "active",
              added_date="2026-09-10", entry_price=20.0, entry_date="2026-09-11",
              stop_loss=18.0, last_price=19.6, unrealized_pnl_pct=-2.0)
        _pool(con, "600003", "持有股C", "active",
              added_date="2026-09-10", entry_price=30.0, entry_date="2026-09-12",
              stop_loss=28.0, take_profit=33.0, last_price=32.4,
              unrealized_pnl_pct=8.0)
        # 1 只观察 + 1 只待买入 (距下沿 1% 触发触价临近)
        _pool(con, "600004", "观察股D", "watch", valid_until="2026-09-30")
        _pool(con, "600005", "待买股E", "pending_entry",
              entry_low=40.0, entry_high=42.0, last_price=40.2,
              valid_until="2026-09-30")

    def test_holdings_three_line_shape(self):
        """裁决2: 覆盖行 + 待行动行(附当期依据) + 一致行; 不许逐只罗列。"""
        con = _mkdb()
        self._seed_holdings(con)
        r = EveningStorylineSection().build(_ctx(con))
        body = r.body
        # 覆盖行: N 只全部在跟踪中 (持有中/观察中/待买入 计数)
        assert "全部在跟踪中" in body
        assert "持有中 3" in body and "观察中 1" in body and "待买入 1" in body
        # 回退路径诚实标注 (跟踪明细表未上线)
        assert "跟踪明细表" in body
        # 待行动行: 接近参考卖出线 + 触价临近, 各附现价/线/距离%
        assert "接近参考卖出线" in body and "持有股A" in body
        assert "9.65" in body and "9.50" in body
        assert "触价临近" in body and "待买股E" in body
        assert "接近目标位" in body and "持有股C" in body
        # 一致行: 不许写「理由仍然成立」(无 consistency_check 数据支撑)
        assert "无待办" in body and "持续监控中" in body
        assert "理由仍然成立" not in body
        # 不逐只罗列: 未触发信号的持有股B不出现在监控文字
        assert "持有股B" not in body

    def test_holdings_no_actions_honest(self):
        con = _mkdb()
        _pool(con, "600001", "持有股A", "active",
              added_date="2026-09-10", entry_price=10.0, entry_date="2026-09-11",
              stop_loss=8.0, last_price=11.0, unrealized_pnl_pct=10.0)
        r = EveningStorylineSection().build(_ctx(con))
        assert "全部在跟踪中" in r.body
        assert "持续监控中" in r.body
        assert "理由仍然成立" not in r.body


class TestStorylineAggregation:
    def _seed_bulk(self, con, n_bought=80, n_pending=40, n_expired=3):
        """模拟 146 只已买入 + 40 只未触价 + 3 只过期的生产量级。"""
        for i in range(n_bought):
            active = i % 2 == 0
            _pool(
                con, f"20{i:04d}", f"买{i}", "active" if active else "exited",
                added_date="2026-09-10", entry_date="2026-09-11",
                entry_price=10.0, stop_loss=9.0,
                last_price=(10.5 if active else None),
                unrealized_pnl_pct=(5.0 if active else None),
                exit_price=(None if active else 10.2),
                exit_date=(None if active else "2026-09-18"),
                total_return_pct=(None if active else (2.0 if i % 3 else -4.0)))
        for i in range(n_pending):
            _pool(con, f"30{i:04d}", f"悬{i}", "pending_entry",
                  added_date="2026-09-18", entry_low=10.0, entry_high=10.5,
                  valid_until="2026-09-30", initial_score=0.5)
        for i in range(n_expired):
            _pool(con, f"40{i:04d}", f"过{i}", "pending_entry",
                  added_date="2026-09-10", entry_low=10.0, entry_high=10.5,
                  valid_until="2026-09-15", initial_score=0.4)

    def test_storyline_aggregated_not_itemized(self):
        """146+ 条输入 → 输出按动作分组聚合, 无逐只行, 有计数行。"""
        con = _mkdb()
        self._seed_bulk(con)
        r = EveningStorylineSection().build(_ctx(con))
        body = r.body
        # 逐只叙事消失 (旧句式「晨报推荐（区间」逐只行)
        assert "晨报推荐（区间" not in body
        # 行数收敛: 聚合后远低于逐只叙事
        non_empty = [ln for ln in body.splitlines() if ln.strip()]
        assert len(non_empty) < 25, f"故事线未收敛: {len(non_empty)} 行"
        # 按动作分组的计数行
        assert "40 只已退出" in body
        assert "胜率" in body
        assert "未触" in body and "继续跟踪" in body
        assert "作废" in body
        # 已退出 盈亏 Top3/Bottom3 各一行
        assert "盈亏前三" in body and "亏损前三" in body

    def test_pending_due_soon_only_listed(self):
        """未触价组: 一行计数 + 仅列有效余期 ≤2 天者。
        (夹具先垫 3 只高分票, 防两只夹具票自己进 Top3 被去重规则滤掉)。"""
        con = _mkdb()
        _seed_candidates(con, 3)
        _pool(con, "500001", "远期权", "pending_entry",
              added_date="2026-09-18", entry_low=10.0, entry_high=10.5,
              valid_until="2026-09-30", initial_score=0.5)
        _pool(con, "500002", "临期股", "pending_entry",
              added_date="2026-09-15", entry_low=10.0, entry_high=10.5,
              valid_until="2026-09-22", initial_score=0.5)  # 余 1 天
        r = EveningStorylineSection().build(_ctx(con))
        assert "5 只候选" in r.body
        assert "临期股" in r.body and "余 1 天" in r.body
        assert "远期权" not in r.body

    def test_no_duplicate_code_across_groups(self):
        """铁原则2: 进了明日推荐 Top3 的票, 故事线未触价组不再重复出现。"""
        con = _mkdb()
        # 该票既是 Top3 候选又余期 ≤2 天 → 旧实现两边都会出现
        _pool(con, "700001", "双现股", "pending_entry",
              added_date="2026-09-18", entry_low=10.0, entry_high=10.5,
              valid_until="2026-09-22", initial_score=0.99,
              added_reason=RANK_REASON)
        picks = TomorrowPicksSection().build(_ctx(con))
        assert "双现股" in picks.body
        story = EveningStorylineSection().build(_ctx(con))
        assert "双现股" not in story.body
        assert "明日推荐" in story.body, "应注明已在明日推荐中, 不静默消失"


class TestSkipEntryAggregated:
    def test_skip_entry_aggregated(self):
        """附录 skip_entry 聚合一行: 同因合并计数, 不逐只刷屏, 不带原始百分比。"""
        actions = [
            {"action_type": "skip_entry", "code": f"00000{i}",
             "reason": "风控闸门拦截: 回撤熔断: 组合回撤 -93.64% (阈值 -8%)"}
            for i in range(10)]
        actions.append({"action_type": "skip_entry", "code": "000099",
                        "reason": "风控闸门拦截: 评分跌破下限"})
        execution = MagicMock()
        feed = MagicMock()
        feed.actions = actions
        execution.get_today_actions.return_value = feed
        ctx = ReportContext(date="2026-09-21", execution_layer=execution)
        r = ActionSummarySection().build(ctx)
        body = r.body
        assert body.count("风控拦截") == 2  # 两种原因各一行
        assert "风控拦截 10 只（原因：回撤熔断）" in body
        assert "风控拦截 1 只（原因：评分跌破下限）" in body
        assert "-93.64" not in body and "阈值" not in body
        assert "000000" not in body, "附录不逐只列代码"


class TestDmEveningReasonConsistent:
    def test_dm_evening_reason_consistent(self):
        """裁决3+展示方向: DM 卡与晚报走同一 _pick_reason, 理由口径统一。"""
        import pandas as pd

        scripts = Path(__file__).resolve().parents[3] / "scripts"
        sys.path.insert(0, str(scripts))
        sys.path.insert(0, str(scripts / "marimo_pages"))
        import build_marimo_report as B

        sig = pd.DataFrame([
            {"rank": 1, "code": "100001", "name": "候选股1",
             "composite_score": 0.9, "buy_range": "10.0~10.5",
             "stop_loss": 9.5, "one_line_reason": RANK_REASON,
             "review_status": "not_queued", "signal_date": "2026-09-21"},
        ])
        with patch.object(B.C, "ops_oneliner", return_value="运维一行"), \
             patch.object(B.C, "latest_signals", side_effect=lambda: sig):
            card = B.build_dm_card("2026-09-21", 7, 7, scripts, period="daily")
        text = "\n".join(e["text"]["content"] for e in card["elements"]
                         if e.get("tag") == "div")
        # DM 理由位 = 单点闸输出, 排名话术 0 处
        assert not RANK_PHRASE_RE.search(text)
        assert GENERATING_TEXT in text
        # 与晚报同函数同输入 → 同输出
        assert _pick_reason({"added_reason": RANK_REASON}) == GENERATING_TEXT

    def test_dm_keeps_real_reason(self):
        """真理由 (非排名话术) 在 DM 卡原样保留 (沿用钉兼容)。"""
        import pandas as pd

        scripts = Path(__file__).resolve().parents[3] / "scripts"
        sys.path.insert(0, str(scripts))
        sys.path.insert(0, str(scripts / "marimo_pages"))
        import build_marimo_report as B

        sig = pd.DataFrame([
            {"rank": 1, "code": "100002", "name": "候选股2",
             "composite_score": 0.8, "buy_range": "9.0~9.5",
             "stop_loss": 8.5, "one_line_reason": "低估值+资金流入",
             "review_status": "pending", "signal_date": "2026-09-21"},
        ])
        with patch.object(B.C, "ops_oneliner", return_value="运维一行"), \
             patch.object(B.C, "latest_signals", side_effect=lambda: sig):
            card = B.build_dm_card("2026-09-21", 7, 7, scripts, period="daily")
        text = "\n".join(e["text"]["content"] for e in card["elements"]
                         if e.get("tag") == "div")
        assert "低估值+资金流入" in text


class TestTopNDeficitHonest:
    def test_fewer_than_3_reported_honestly(self):
        """宁缺毋滥: 只有 2 只候选就报 2 只, 不凑满。"""
        con = _mkdb()
        _seed_candidates(con, 2)
        r = TomorrowPicksSection().build(_ctx(con))
        assert r.body.count("◆") == 2
        assert "另有" not in r.body
