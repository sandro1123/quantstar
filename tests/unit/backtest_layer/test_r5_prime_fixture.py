# -*- coding: utf-8 -*-
"""R-5′ 判据落地——snapshot 分子行集合守恒（fixture 驱动，双态 + 反向钉）。

判据族（Beta gate v1 元规则）：判据若在「系统坏着」时也通过，它不是判据。
旧判据 nav.position_value == SUM(...) 在零持仓期 0=0 恒真 → 作废。
本文件 = 新判据的可执行形态：隔离 in-memory 库（真 DDL 夹具）真实调用
snapshot_positions_for_date，测「机器行为」而非「世界状态」。

三问自答（机治钉三问）：
1) 它守护的接线坏时会红吗？——会（行集合缺 exit_pending / 混入 exited / 守恒破 → 红）
2) 篡改验证（对方做的人）——统筹已实测 4 只三态 rows=4、零仓 rows=0、只 active 对照漏 mv
3) 它不宣称做的事会误伤吗？——反向钉钉的是「不该红的场景不红」（cron 态监控器就绪不红）

夹具日期纪律：动态 today-N（qs-gate-full-recon 20261007 既判罪），全文件零硬编码日期。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from backtest_layer.paper_trading.settlement import snapshot_positions_for_date

# ---- 夹具：真 DDL（stock_pool + paper_positions_snapshot），日期动态 ----


def _today_n(n: int) -> str:
    """today-N 自然日（fixture 用，非交易日历——被测函数不校验交易日）。"""
    return (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture()
def snap_db():
    """隔离 in-memory 库，真 DDL（列集取自生产 PRAGMA，判例：夹具用真 schema）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE stock_pool (
            code TEXT PRIMARY KEY, name TEXT, status TEXT,
            entry_price REAL, last_price REAL, position_qty REAL,
            unrealized_pnl REAL, exit_date TEXT)"""
    )
    conn.execute(
        """CREATE TABLE paper_positions_snapshot (
            date TEXT, code TEXT, shares REAL, mv REAL, weight REAL,
            entry_price REAL, unrealized_pnl REAL, created_at TEXT,
            PRIMARY KEY (date, code))"""
    )
    yield conn
    conn.close()


class _DB:
    def __init__(self, conn):
        self._c = conn

    def get_connection(self):
        return self._c


# ---- 态一：有持仓（三态进 + 两态排） ----


def test_fixture_three_statuses_in_two_statuses_out(snap_db):
    """分子行集合：exit_pending/pending_entry/active 进，exited/watch 排除。

    这正是 09-28 假回撤（-96.98%）的病根两面：漏 exit_pending（旧）/混入 exited（反面）。
    """
    d = _today_n(1)
    rows = [
        ("300001", "exit_pending", 10.0, 11.0, 100),   # 进（T+1 结算当日态）
        ("300002", "pending_entry", 20.0, 21.0, 200),  # 进（待入）
        ("300003", "active", 5.0, 6.0, 300),           # 进
        ("300004", "exited", 99.0, 99.0, 999),         # 排（已退出）
        ("300005", "watch", 88.0, 88.0, 888),          # 排（仅观察）
    ]
    for code, st, entry, last, qty in rows:
        snap_db.execute(
            "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty) "
            "VALUES (?,?,?,?,?)",
            (code, st, entry, last, qty))
    snap_db.commit()

    res = snapshot_positions_for_date(_DB(snap_db), d)
    assert res["rows"] == 3, f"三态应产 3 行，实际 {res}"
    got = {r["code"] for r in snap_db.execute(
        "SELECT code FROM paper_positions_snapshot WHERE date=?", (d,))}
    assert got == {"300001", "300002", "300003"}, f"分子行集合错: {got}"


def test_fixture_mv_conservation_exact(snap_db):
    """数值守恒：snapshot.mv 合计 == Σ(last_price×position_qty)，隔离环境零容差。"""
    d = _today_n(2)
    rows = [("310001", "exit_pending", 10.0, 11.0, 100),
            ("310002", "pending_entry", 20.0, 21.0, 200)]
    for code, st, entry, last, qty in rows:
        snap_db.execute(
            "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty) "
            "VALUES (?,?,?,?,?)", (code, st, entry, last, qty))
    snap_db.commit()
    snapshot_positions_for_date(_DB(snap_db), d)
    total = snap_db.execute(
        "SELECT SUM(mv) FROM paper_positions_snapshot WHERE date=?", (d,)).fetchone()[0]
    expect = 11.0 * 100 + 21.0 * 200
    assert total == pytest.approx(expect, abs=1e-9), f"守恒破: {total} != {expect}"


def test_fixture_weight_sums_to_one(snap_db):
    """weight 归一（资金权重面）：Σweight==1（有持仓时）。"""
    d = _today_n(3)
    for code, st, entry, last, qty in [
        ("320001", "exit_pending", 10.0, 12.0, 100),
        ("320002", "active", 20.0, 30.0, 100),
    ]:
        snap_db.execute(
            "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty) "
            "VALUES (?,?,?,?,?)", (code, st, entry, last, qty))
    snap_db.commit()
    snapshot_positions_for_date(_DB(snap_db), d)
    w = snap_db.execute(
        "SELECT SUM(weight) FROM paper_positions_snapshot WHERE date=?", (d,)).fetchone()[0]
    assert w == pytest.approx(1.0, abs=1e-6)


# ---- 态二：零持仓（诚实空 = 现状正确行为的回归钉） ----


def test_zero_position_honest_empty(snap_db):
    """零持仓：全表无三值行 → rows=0 诚实空，不报错、不写行（统筹 10-09 亲测形态）。"""
    d = _today_n(1)
    snap_db.execute(
        "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty) "
        "VALUES ('390001','exited',1,1,0)")
    snap_db.commit()
    res = snapshot_positions_for_date(_DB(snap_db), d)
    assert res["rows"] == 0 and "reason" not in res or res.get("reason") != "read_failed"
    n = snap_db.execute(
        "SELECT COUNT(*) FROM paper_positions_snapshot WHERE date=?", (d,)).fetchone()[0]
    assert n == 0


def test_zero_position_table_empty(snap_db):
    """连 stock_pool 都空（冷启动）：诚实空不炸。"""
    d = _today_n(1)
    res = snapshot_positions_for_date(_DB(snap_db), d)
    assert res["rows"] == 0


# ---- 缺价退化（真函数的 skipped 语义） ----


def test_missing_price_skipped_not_valued(snap_db):
    """last_price 缺 → skip 该行（不写 mv=0 假仓），有价行照写。"""
    d = _today_n(1)
    snap_db.executemany(
        "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty) "
        "VALUES (?,?,?,?,?)",
        [("330001", "exit_pending", 10.0, None, 100),   # 无价 → skip
         ("330002", "active", 20.0, 5.0, 100)])          # 有价 → 写
    snap_db.commit()
    res = snapshot_positions_for_date(_DB(snap_db), d)
    assert res["rows"] == 1 and res["skipped"] == 1
    got = {r["code"] for r in snap_db.execute(
        "SELECT code FROM paper_positions_snapshot WHERE date=?", (d,))}
    assert got == {"330002"}
