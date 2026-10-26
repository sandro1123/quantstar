# -*- coding: utf-8 -*-
"""REC 模块4 (2026-09-21): recommendation_tracking — 推荐全周期跟踪。

用户定谳: 执行层=推荐跟踪器非交易模拟器。治「一锤子买卖」——对推荐过的
股票负责到底: 持续跟踪+一致连贯不矛盾的建议, 直到明确止盈/止损/放弃。
**不依赖 paper_trading 持仓, 依赖推荐记录本身** (推过就跟踪, 不问买没买)。

归属层 = 执行层 (tracking/ 子目录, pnl_tracker 同居; 用户定谳的直接落点)。
表名无撞 (PRAGMA 扫过 92 表: post_exit_tracking=退出后检查点语义不同,
recommendation_retrospect=结算面)。

统筹三提醒:
1. 状态机唯一入口守卫 (pool_position_guard 模式) — 状态迁移全走
   transition(), 多入口写状态=防线不存在;
2. 建议依据链: 每条建议 (维持/加仓/减仓/止损/放弃) 附当期依据
   (非模板) + 依据演变可追溯; 扭转时据实给新证据, 措辞「情况变化→建议更新」
   非「认错」(用户定谳: 据实更新非认错);
3. 一致性守护: 每日问「N 天前推荐它的理由今天还成立吗」— 翻脸必须
   显式给依据, 无声翻脸=守卫 RED。

状态机: recommended → tracking → building (建仓观察) → holding →
        exiting (建议止盈/止损) → closed (跟踪结束)
"""
import json
import sqlite3

import pytest

from common.utils import bj_date

from common.database import Database


# 判例#8: RED 须 AssertionError 非 ImportError。
def test_tracker_module_exists():
    try:
        import execution_layer.tracking.recommendation_tracker  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"recommendation_tracker 未建: {e}") from e


# 判例#8
def test_module_exists():
    try:
        import execution_layer.tracking.recommendation_tracker  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"recommendation_tracker 未建: {e}") from e


def _db():
    db = Database(":memory:")
    conn = db.get_connection()
    from execution_layer.tracking.recommendation_tracker import TRACKING_DDL
    conn.executescript(TRACKING_DDL)
    conn.commit()
    return db, conn


# ---- 状态机 ----

def test_start_tracking_from_recommendation():
    """推荐落地即开始跟踪 (不问买没买 — 依赖推荐记录本身)。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    tid = t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
                  verdict="推荐", reason="量价齐升+龙头地位")
    row = t.get("600001")
    assert row is not None, "推荐落地须立即进跟踪 (一锤子买卖的反面)"
    assert row["state"] == "recommended"
    assert row["rationale_id"] == "r1"


def test_state_machine_valid_transitions():
    """合法迁移链: recommended→tracking→building→holding→exiting→closed。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
            verdict="推荐", reason="x")
    for nxt, basis in (("tracking", "首个交易日跟踪中"),
                       ("building", "接近买入区间下沿"),
                       ("holding", "已建仓观察中"),
                       ("exiting", "触达止盈区间"),
                       ("closed", "止盈了结")):
        t.transition("600001", nxt, basis=basis)
    assert t.get("600001")["state"] == "closed"


def test_illegal_transition_rejected():
    """非法迁移 (recommended→holding 跳级) 拒绝且留痕, 不静默。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
            verdict="推荐", reason="x")
    with pytest.raises(ValueError, match="非法迁移"):
        t.transition("600001", "holding", basis="跳级尝试")
    assert t.get("600001")["state"] == "recommended", "非法迁移不得生效"


def test_transition_appends_basis_chain():
    """每次迁移追加依据链 (建议依据可追溯, 依据链=灵魂)。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
            verdict="推荐", reason="量价齐升")
    t.transition("600001", "tracking", basis="首日观察无异常")
    t.transition("600001", "building", basis="回踩买入区间下沿 10.2")
    row = t.get("600001")
    chain = json.loads(row["basis_chain"])
    assert len(chain) == 3, f"依据链须 3 节点 (start+2迁移), 实得 {len(chain)}"
    assert chain[0]["basis"] == "量价齐升"
    assert chain[2]["basis"] == "回踩买入区间下沿 10.2"
    assert chain[2]["from_state"] == "tracking"


def test_daily_advice_requires_current_basis():
    """每日建议 (维持/加仓/减仓/止损/放弃) 必须附当期依据 — 空依据拒写。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
            verdict="推荐", reason="x")
    t.transition("600001", "tracking", basis="观察中")
    with pytest.raises(ValueError, match="依据"):
        t.record_advice("600001", advice="维持", basis="")  # 空模板话术拒收
    t.record_advice("600001", advice="维持", basis="缩量整理, 买入区间仍有效")
    n = conn.execute("SELECT COUNT(*) FROM recommendation_tracking_advice "
                     "WHERE code='600001'").fetchone()[0]
    assert n == 1


def test_advice_reversal_requires_new_evidence():
    """翻脸必须显式新证据: 推荐→放弃 的建议须 evidence 字段, 否则拒。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-21", rationale_id="r1",
            verdict="推荐", reason="x")
    t.transition("600001", "tracking", basis="观察中")
    with pytest.raises(ValueError, match="新证据"):
        t.record_advice("600001", advice="放弃", basis="不想推了")  # 无新证据的翻转
    # 据实更新: 给新证据 → 收 (措辞=情况变化→建议更新, 非认错)
    t.record_advice("600001", advice="放弃",
                    basis="跌破止损线 9.5 且放量下行",
                    evidence="break_stop_loss@9.5", is_reversal=True)
    row = conn.execute(
        "SELECT advice, evidence, is_reversal FROM recommendation_tracking_advice "
        "WHERE code='600001' ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "放弃" and row[1] == "break_stop_loss@9.5" and row[2] == 1


def test_consistency_check_flags_silent_flip():
    """一致性守护: 昨日推荐理由今日不成立时, 须有 advice 或 reversal 留痕;
    否则 consistency_check 报 silent_flip (每日自问的载体)。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    today = bj_date()
    t.start(code="600001", rec_date=today, rationale_id="r1",
            verdict="推荐", reason="x")
    # 今日 invalidation 触发但无任何建议留痕 → silent_flip
    report = t.consistency_check(code="600001", asof=today,
                                 still_valid=False,
                                 invalidation_reason="跌破9.5")
    assert report["silent_flip"] is True, \
        "理由失效且无建议留痕 = 无声翻脸, 守卫必须报"
    # 补建议后复检 → 干净
    t.record_advice("600001", advice="止损", basis="跌破9.5",
                    evidence="break_stop_loss@9.5", is_reversal=True)
    report2 = t.consistency_check(code="600001", asof=today,
                                  still_valid=False,
                                  invalidation_reason="跌破9.5")
    assert report2["silent_flip"] is False


def test_coverage_metric():
    """跟踪覆盖率指标: 推荐的每只股是否每日被持续关注。"""
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    db, conn = _db()
    t = RecommendationTracker(db)
    today = bj_date()
    t.start(code="600001", rec_date=today, rationale_id="r1",
            verdict="推荐", reason="x")
    t.start(code="600002", rec_date=today, rationale_id="r2",
            verdict="推荐", reason="y")
    t.record_advice("600001", advice="维持", basis="正常")
    cov = t.coverage(asof=today)
    assert cov["tracked_total"] == 2
    assert cov["advised_today"] == 1
    assert cov["coverage_ratio"] == 0.5, "600002 未被建议 = 覆盖缺口显形"
