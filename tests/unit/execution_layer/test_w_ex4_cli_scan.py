# -*- coding: utf-8 -*-
"""EX-4 CLI 接线+扫描域收窄 — RED 先行 (:memory:, 无网络)。

派单 sm-dbdcf773 两件:
  ① EX-4 CLI 接线: advisor 闭环接 ExecutionLayer.intraday_scan() 调用面
     (扫描后段 = 15:00 收盘轮自动跑 pool_maintenance + 三态清单落 action_records)
  ② 扫描域收窄: intraday 域 = advisor.scan_codes 单一口径 (pending+watch+active);
     收窄前后对比账 = get_by_status 全域 vs scan_codes 域

红线: monitor._scan 取数序零触碰 (三序契约面); 接线全落 CLI/ExecutionLayer 层。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution_layer.tables import (  # 正典 DDL: 夹具不再自己手写一份
    ACTION_RECORDS_DDL, STOCK_POOL_DDL)

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from execution_layer.advisor import recommendation_advisor as ra  # noqa: E402
from execution_layer.scripts import ex4_cli_scan as cli  # noqa: E402  ← RED: 模块不存在


class _DB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 正典 DDL (同 test_w_ex4_advisor: 原手写窄表屏蔽了 action_records 的
        # source/action_type 两条 CHECK, 是 AUD-029 假绿的温床)
        self._conn.execute(STOCK_POOL_DDL)
        self._conn.execute(ACTION_RECORDS_DDL)
        self._conn.execute("""
            CREATE TABLE agent_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT,
                code TEXT, verdict TEXT, confidence REAL,
                created_at TEXT, reasoning TEXT)""")
        self._conn.commit()

    def get_connection(self):
        return self._conn


@pytest.fixture
def db():
    return _DB()


def _seed_pool(conn, code, **kw):
    cols = {"code": code, "name": code, "status": "pending_entry",
            "source": "strategy", "added_date": "2026-09-10",
            "initial_score": 0.6, "entry_low": 10.0, "entry_high": 10.5,
            "valid_until": "2099-01-01", "last_price": 10.2}
    cols.update(kw)
    keys = ",".join(cols)
    ph = ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO stock_pool ({keys}) VALUES ({ph})", tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# ② 扫描域收窄 — scan_codes 单一口径 + 对比账
# ---------------------------------------------------------------------------

def test_scan_codes_domain(db):
    """扫描域 = pending+watch+active 三态 (exited 不扫)。"""
    conn = db.get_connection()
    _seed_pool(conn, "A1", status="pending_entry")
    _seed_pool(conn, "A2", status="watch")
    _seed_pool(conn, "A3", status="active")
    _seed_pool(conn, "A4", status="exited", exit_reason="止损")
    codes = ra.scan_codes(db)
    assert set(codes) == {"A1", "A2", "A3"}   # exited 出域


def test_domain_narrowing_account(db):
    """收窄前后对比账: full_scope vs narrowed 差 = exited+其他态数量。"""
    conn = db.get_connection()
    _seed_pool(conn, "B1", status="pending_entry")
    _seed_pool(conn, "B2", status="watch")
    _seed_pool(conn, "B3", status="exited")
    _seed_pool(conn, "B4", status="exit_pending")
    acct = cli.domain_account(db)
    assert acct["full"] >= 4
    assert acct["narrowed"] == 2            # pending+watch
    assert acct["full"] - acct["narrowed"] == acct["excluded"]


# ---------------------------------------------------------------------------
# ① CLI 闭环段 — 扫描后段 advisor 段
# ---------------------------------------------------------------------------

def test_advisor_segment_runs_maintenance_and_records(db):
    """advisor 段: 到期降级 + 三态清单落 action_records (advisory 三态留痕)。"""
    conn = db.get_connection()
    _seed_pool(conn, "C1", status="pending_entry", valid_until="2026-09-01",
               entry_low=10.0, entry_high=10.5)      # 到期 → 降级 watch
    _seed_pool(conn, "C2", status="watch", last_price=9.1,
               entry_low=10.0)                        # 跌破低点 8% → exited
    n = cli.advisor_segment(db, today="2026-09-11", now_hhmm="15:05")
    assert n >= 2
    s1 = conn.execute("SELECT status FROM stock_pool WHERE code='C1'").fetchone()
    s2 = conn.execute("SELECT status FROM stock_pool WHERE code='C2'").fetchone()
    assert s1["status"] == "watch" and s2["status"] == "exited"
    # AUD-029: source 原为 'advisor' —— 那个值违反 action_records 的 source CHECK,
    # 只在旧的手写窄表夹具(无 CHECK)里才写得进去 = 断言把病值锁住了。
    # 现为 'execution'(层名);「是 advisor 干的」由 action_type 承载。
    # 这里保留 source 过滤而不是删掉 = 顺带把合法值也锁进断言, 防 AUD-029 回退。
    types = [r[0] for r in conn.execute(
        "SELECT action_type FROM action_records WHERE source='execution'").fetchall()]
    assert "pool_downgrade" in types and "pool_exit" in types


def test_advisor_segment_tri_state_ledger(db):
    """三态清单留痕: 每候选一行 advisory_state (buy_now/wait_watch/pass)。"""
    conn = db.get_connection()
    _seed_pool(conn, "D1", status="pending_entry", last_price=10.2)   # buy_now
    _seed_pool(conn, "D2", status="pending_entry", last_price=11.9)   # wait_watch
    cli.advisor_segment(db, today="2026-09-11", now_hhmm="15:05")
    rows = dict(conn.execute(
        "SELECT code, reason FROM action_records WHERE action_type='advisory_state'"
    ).fetchall())
    assert "D1" in rows and "buy_now" in rows["D1"]
    assert "D2" in rows and "wait_watch" in rows["D2"]


def test_advisor_segment_after_close_only(db):
    """收盘轮形态: now 早于 15:00 → 不跑 (盘中扫描不做池维护, 防盘中状态漂移)。"""
    conn = db.get_connection()
    _seed_pool(conn, "E1", status="pending_entry", valid_until="2026-09-01")
    n = cli.advisor_segment(db, today="2026-09-11", now_hhmm="10:30")
    assert n == 0
    row = conn.execute("SELECT status FROM stock_pool WHERE code='E1'").fetchone()
    assert row["status"] == "pending_entry"   # 未动


def test_cli_scan_entry_composes(db, monkeypatch):
    """CLI 入口编排: run() = 域账 + advisor 段 (scan 面由 intraday_scan 主入口持, 本测只验编排序)。"""
    calls = []
    monkeypatch.setattr(cli, "domain_account", lambda d: {"full": 5, "narrowed": 3, "excluded": 2, "by_status": {}})
    monkeypatch.setattr(cli, "advisor_segment",
                        lambda d, today=None, now_hhmm=None: calls.append("advisor") or 3)
    rc = cli.run(db, today="2026-09-11", now_hhmm="15:05")
    assert calls == ["advisor"]
    assert rc == 0
    # 早于收盘 → advisor 段仍会跑吗? run 以 now_hhmm 传入 advisor 判收盘 (默认 15:00)
    calls.clear()
    rc = cli.run(db, today="2026-09-11", now_hhmm="10:30")
    assert calls == []      # 盘中不跑 advisor 段
