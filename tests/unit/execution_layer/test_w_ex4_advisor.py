# -*- coding: utf-8 -*-
"""EX-4 三态建议引擎 — RED 先行验收测试 (:memory:, 无网络, LLM 缺席降规则文案)。

四件 (派单 sm-4382c008):
  A. RecommendationAdvisor 三态判定 buy_now / wait_watch / pass + 条件单四件套
  B. 池退出: 推荐池条件单到期未触发→降级回跟踪池; 跟踪池退出三规则
     (跌破观察期低点 X%=8% / 成交额萎缩至入围一半 / 连续 N=10 日无信号)
  C. 推荐位收口: N=clamp(3,8) 断层浮动; pool-first (跟踪池优先升推荐) +
     composite_score 补位 + risk 强负面前置否决 (score<-0.5 且 conf>0.7)
  D. intraday_scan 域收窄 (优先推荐池+跟踪池)
契约: 三态输出 JSON (report_layer 四行结构呈现面)。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from execution_layer.tables import (  # 正典 DDL: 夹具不再自己手写一份
    ACTION_RECORDS_DDL, STOCK_POOL_DDL)

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from execution_layer.advisor import recommendation_advisor as ra  # noqa: E402 ← RED: 模块不存在


# ---------------------------------------------------------------------------
# 夹具: :memory: 库 + stock_pool 真表 (复用 tables.py 建表 DDL)
# ---------------------------------------------------------------------------

class _DB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        conn = self._conn
        # 正典 DDL (原为手写窄表: 列面缺 exit_price/total_return_pct 等, 且
        # action_records 无任何 CHECK -> source='advisor' 在此一直写得进去,
        # 测试恒绿而生产必炸, 正是 AUD-029 被藏了这么久的原因)
        conn.execute(STOCK_POOL_DDL)
        conn.execute(ACTION_RECORDS_DDL)
        conn.execute("""
            CREATE TABLE agent_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT,
                code TEXT, verdict TEXT, confidence REAL,
                created_at TEXT, reasoning TEXT)""")
        conn.commit()

    def get_connection(self):
        return self._conn


@pytest.fixture
def db():
    return _DB()


def _add_pool(conn, code, **kw):
    cols = {"code": code, "name": code, "status": "pending_entry",
            "source": "strategy", "added_date": "2026-09-08",
            "initial_score": 0.6, "entry_low": 10.0, "entry_high": 10.5,
            "valid_until": "2026-09-17"}
    cols.update(kw)
    keys = ",".join(cols.keys())
    ph = ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO stock_pool ({keys}) VALUES ({ph})",
                 tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# A. 三态判定
# ---------------------------------------------------------------------------

def test_tri_buy_now_with_order_plan(db):
    """buy_now: 附条件单四件套 (建议价区/触发/目标/止损)。"""
    conn = db.get_connection()
    _add_pool(conn, "000001", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", initial_score=0.7)
    out = ra.advise(db, "000001", last_price=10.2, today="2026-09-09")
    assert out["state"] == "buy_now"
    plan = out["order_plan"]
    assert {"entry_low", "entry_high", "trigger", "target", "stop_loss"} <= set(plan)
    assert plan["entry_low"] == 10.0 and plan["entry_high"] == 10.5
    assert plan["stop_loss"] > 0 and plan["target"] > plan["entry_high"]


def test_tri_wait_watch_with_condition_and_expiry(db):
    """wait_watch: 附条件+有效期 (回踩线, N 日窗口)。"""
    conn = db.get_connection()
    _add_pool(conn, "000002", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", initial_score=0.6)
    # 现价高于区间上限 → 等回踩
    out = ra.advise(db, "000002", last_price=11.9, today="2026-09-09")
    assert out["state"] == "wait_watch"
    assert "condition" in out and "valid_days" in out
    assert out["valid_days"] > 0


def test_tri_pass_with_reason(db):
    """pass: 条件单过期 (valid_until<today) 且未触发 → pass 附原因。"""
    conn = db.get_connection()
    _add_pool(conn, "000003", valid_until="2026-09-01", entry_low=10.0,
              entry_high=10.5, initial_score=0.6)
    out = ra.advise(db, "000003", last_price=12.0, today="2026-09-09")
    assert out["state"] == "pass"
    assert "expired" in out["reason"] or "过期" in out["reason"]


def test_tri_price_in_zone_low_side_buy(db):
    """价在区间内但近下沿 + 分数高 → buy_now (价区条件单可即时成立)。"""
    conn = db.get_connection()
    _add_pool(conn, "000004", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", initial_score=0.75)
    out = ra.advise(db, "000004", last_price=10.1, today="2026-09-09")
    assert out["state"] == "buy_now"


def test_tri_json_serializable(db):
    """三态输出 JSON 可序列化 (report_layer 四行呈现面契约)。"""
    conn = db.get_connection()
    _add_pool(conn, "000005", valid_until="2099-01-01")
    out = ra.advise(db, "000005", last_price=10.2, today="2026-09-09")
    s = json.dumps(out, ensure_ascii=False)
    assert json.loads(s)["state"] in ("buy_now", "wait_watch", "pass")


def test_tri_risk_veto(db):
    """risk 前置否决: 最新 agent 预测 verdict=不推荐/警惕 且 conf>0.7 → pass。"""
    conn = db.get_connection()
    _add_pool(conn, "000006", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", initial_score=0.7)
    conn.execute(
        "INSERT INTO agent_predictions (agent_name, code, verdict, confidence, created_at) "
        "VALUES ('entry_check', '000006', '不推荐', 0.85, '2026-09-09')")
    conn.commit()
    out = ra.advise(db, "000006", last_price=10.2, today="2026-09-09")
    assert out["state"] == "pass"
    assert "risk" in out["reason"].lower() or "否决" in out["reason"] or "不推荐" in out["reason"]


# ---------------------------------------------------------------------------
# B. 池退出
# ---------------------------------------------------------------------------

def test_pool_exit_recommend_expired_to_watch(db):
    """B1: 推荐池条件单到期未触发 → 降级回跟踪池 + 降级原因落 exit_reason 域 + action 留痕。"""
    conn = db.get_connection()
    _add_pool(conn, "100001", status="pending_entry", valid_until="2026-09-01",
              entry_low=10.0, entry_high=10.5)
    n = ra.pool_maintenance(db, today="2026-09-09")
    assert n >= 1
    row = conn.execute(
        "SELECT status, watch_reason FROM stock_pool WHERE code='100001'").fetchone()
    assert row["status"] == "watch"           # 降级回跟踪池
    assert row["watch_reason"] is not None   # 附降级原因


def test_pool_exit_watch_below_observation_low(db):
    """B2: 跟踪池跌破观察期低点 X%=8% → 退出 (exit_reason 落表)。"""
    conn = db.get_connection()
    _add_pool(conn, "100002", status="watch", entry_low=10.0, entry_high=10.5,
              added_date="2026-09-08", valid_until="2099-01-01")
    # 观察期低点 = 入池后最低价; 用 last_price 模拟跌破 8%
    conn.execute("UPDATE stock_pool SET last_price=9.1 WHERE code='100002'")
    conn.commit()
    n = ra.pool_maintenance(db, today="2026-09-09")
    row = conn.execute(
        "SELECT status, exit_reason FROM stock_pool WHERE code='100002'").fetchone()
    assert row["status"] == "exited"
    assert "低点" in (row["exit_reason"] or "") or "8" in (row["exit_reason"] or "")


def test_pool_exit_watch_no_signal_streak(db):
    """B3: 跟踪池连续 N=10 日无信号 → 退出 (latest_signal_date 距今>10)。"""
    conn = db.get_connection()
    _add_pool(conn, "100003", status="watch", added_date="2026-08-20",
              latest_signal_date="2026-08-20", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01")
    n = ra.pool_maintenance(db, today="2026-09-09")
    row = conn.execute(
        "SELECT status, exit_reason FROM stock_pool WHERE code='100003'").fetchone()
    assert row["status"] == "exited"
    assert "无信号" in (row["exit_reason"] or "") or "signal" in (row["exit_reason"] or "")


def test_pool_exit_params_configurable(db):
    """B4: 参数 config 可调 — X/N 从 config dict 读, 非写死。"""
    cfg = ra.DEFAULT_EXIT_CONFIG.copy()
    cfg["no_signal_days"] = 3
    conn = db.get_connection()
    _add_pool(conn, "100004", status="watch", added_date="2026-09-01",
              latest_signal_date="2026-09-01", entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01")
    n = ra.pool_maintenance(db, today="2026-09-09", config=cfg)
    row = conn.execute(
        "SELECT status FROM stock_pool WHERE code='100004'").fetchone()
    assert row["status"] == "exited"  # 8 天 > 3 日即退 (默认 10 不退)


# ---------------------------------------------------------------------------
# C. 推荐位收口
# ---------------------------------------------------------------------------

def test_promotion_pool_first_and_composite_backfill(db):
    """C1: 跟踪池优先升推荐 (pool-first), composite_score 补位, N=clamp(3,8)。"""
    conn = db.get_connection()
    # 跟踪池 2 只 (池内优先) + 候选 3 只 (composite 补位)
    _add_pool(conn, "200001", status="watch", initial_score=0.5,
              current_score=0.9, entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", added_date="2026-09-01")
    _add_pool(conn, "200002", status="watch", initial_score=0.5,
              current_score=0.85, entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", added_date="2026-09-01")
    candidates = [
        {"code": "300001", "composite_score": 0.95, "entry_low": 10.0, "entry_high": 10.5},
        {"code": "300002", "composite_score": 0.60, "entry_low": 10.0, "entry_high": 10.5},
        {"code": "300003", "composite_score": 0.88, "entry_low": 10.0, "entry_high": 10.5},
    ]
    plan = ra.promote_recommendations(db, watch_candidates=candidates,
                                      n_target=3, today="2026-09-09")
    # 推荐位: 2 watch 升 + 1 composite 最高者 (300001) 补位
    rows = {r["code"]: r for r in conn.execute(
        "SELECT code, status FROM stock_pool WHERE status='pending_entry'").fetchall()}
    assert "200001" in rows and "200002" in rows   # pool-first
    assert "300001" in rows                         # composite 补位最高
    assert "300002" not in rows                      # 0.60 不入
    assert len(plan["promoted"]) == 2
    assert len(plan["backfilled"]) == 1


def test_promotion_n_clamp(db):
    """C2: N=clamp(3,8) — 目标位数夹在 [3,8]。"""
    assert ra.clamp_n(1) == 3
    assert ra.clamp_n(5) == 5
    assert ra.clamp_n(12) == 8


def test_promotion_risk_veto(db):
    """C3: risk 强负面 (score<-0.5 且 conf>0.7) 前置否决 — 负面候选不升。"""
    conn = db.get_connection()
    _add_pool(conn, "200003", status="watch", initial_score=0.5,
              current_score=0.92, entry_low=10.0, entry_high=10.5,
              valid_until="2099-01-01", added_date="2026-09-01")
    conn.execute(
        "INSERT INTO agent_predictions (agent_name, code, verdict, confidence, created_at) "
        "VALUES ('entry_check', '200003', '不推荐', 0.9, '2026-09-09')")
    conn.commit()
    plan = ra.promote_recommendations(db, watch_candidates=[], n_target=3,
                                      today="2026-09-09")
    rows = conn.execute(
        "SELECT status FROM stock_pool WHERE code='200003'").fetchone()
    assert rows["status"] == "watch"  # 否决未升
    assert any("200003" in (v.get("code", "") if isinstance(v, dict) else str(v))
               for v in plan.get("vetoed", [])) or len(plan.get("vetoed", [])) >= 1


def test_promotion_gap_float(db):
    """C4: 断层浮动 — 候选分数断层大 (如前3名明显高) 时 N 跟着断层走 (不多带低分)。"""
    conn = db.get_connection()
    # watch 空库, 候选: 0.95/0.93/0.91/0.50 (断层在 0.91→0.50)
    cands = [
        {"code": f"40000{i}", "composite_score": s, "entry_low": 10.0, "entry_high": 10.5}
        for i, s in enumerate([0.95, 0.93, 0.91, 0.50], 1)
    ]
    plan = ra.promote_recommendations(db, watch_candidates=cands, n_target=4,
                                      today="2026-09-09")
    # 断层浮动: 只取断层以上 3 只, 不带 0.50
    rows = [r["code"] for r in conn.execute(
        "SELECT code FROM stock_pool WHERE status='pending_entry'").fetchall()]
    assert "400004" not in rows
    assert len(rows) == 3
