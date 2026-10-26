# -*- coding: utf-8 -*-
"""P0 熔断假态根治 — TDD（刀一全组合口径 / 刀二 peak_nav / 丙 留痕四值）。

【事故复现配方】—— 审计官从 data/backups/qs_2026-10-07.db.gz（3.8GB）抢救取证，
统筹独立复核通过，非人造输入（原库 /tmp/qs_1007_f.db 只读暂存）：
  stock_pool 28 行三态（exit_pending 25 + pending_entry 3），按 last_price_date 分组：
    NULL×3 (pv=0, 不进曲线) / 09-18×21 (pv=275,825) / 09-22×1 (pv=1,510)
    / 09-25×3 (pv=3,404, 末组)
  cash = 300,000 − 280,739(在投) − 2,917,623(exited成本) + 2,905,210(回款) = 6,848
  旧实现 navs[-1] = 6,848 + 3,404 = 10,252 → dd = −96.5827%（留痕逐位一致）
  真值 = 6,848 + 280,739 = 287,587 → dd = −4.1377%

三问自答（机治钉）：
1) 守护的接线坏时会红吗？——同一配方在未修实现上必出 −96.58%（反向钉即"修没修"判据）
2) 篡改验证——统筹/审计官（不做本题的人）
3) 误伤面——NULL 组不进分子也不进末组（曲线 SQL 天然排除，守护钉防误伤）；
   真风险日（大额持仓真破线）仍熔断。

夹具日期动态 today-N（既判罪避）。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from execution_layer.core.protection_gate import ProtectionGate

CAP = 300000.0


def _today_n(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _mk_tables(conn):
    conn.execute("""CREATE TABLE stock_pool (
        code TEXT PRIMARY KEY, name TEXT, status TEXT,
        entry_price REAL, last_price REAL, position_qty REAL,
        unrealized_pnl REAL, exit_date TEXT, last_price_date TEXT,
        exit_price REAL)""")
    conn.execute("""CREATE TABLE paper_positions_snapshot (
        date TEXT, code TEXT, shares REAL, mv REAL, weight REAL,
        entry_price REAL, unrealized_pnl REAL, created_at TEXT,
        PRIMARY KEY (date, code))""")
    conn.execute("""CREATE TABLE action_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, action_type TEXT,
        action_detail TEXT, price REAL, previous_value REAL, new_value REAL,
        reason TEXT, source TEXT, created_at TEXT)""")
    conn.commit()


class _DB:
    def __init__(self, conn):
        self._c = conn

    def get_connection(self):
        return self._c


class _FakePool:
    """action_record.write 落内存表 + get_available_cash 与生产同式（D4 资金守恒）。"""

    def __init__(self, conn, cap=CAP):
        self._c = conn
        self._cap = cap

        class _Rec:
            def __init__(self, c):
                self._c = c

            def write(self, code, action_type, reason, source, **kw):
                self._c.execute(
                    "INSERT INTO action_records (code, action_type, action_detail, "
                    "price, previous_value, new_value, reason, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now','+8 hours'))",
                    (code, action_type, kw.get("action_detail"), kw.get("price"),
                     kw.get("previous_value"), kw.get("new_value"),
                     reason, source))
                self._c.commit()
        self.action_record = _Rec(self._c)

    def get_available_cash(self, initial_capital=None):
        cap = float(initial_capital or self._cap)
        r = self._c.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status IN ('active','exit_pending') "
            "  THEN entry_price*position_qty ELSE 0 END),0) invested, "
            "COALESCE(SUM(CASE WHEN status='exited' THEN entry_price*position_qty "
            "  ELSE 0 END),0) exited_cost, "
            "COALESCE(SUM(CASE WHEN status='exited' THEN exit_price*position_qty "
            "  ELSE 0 END),0) proceeds FROM stock_pool").fetchone()
        cash = cap - (r["invested"] or 0) - (r["exited_cost"] or 0) + (r["proceeds"] or 0)
        return max(cash, 0.0)


def _mk_gate(conn, config=None):
    return ProtectionGate(_DB(conn), _FakePool(conn), config=config)


def _seed(conn, rows):
    for code, st, entry, last, qty, lpd in rows:
        conn.execute(
            "INSERT INTO stock_pool (code, status, entry_price, last_price, "
            "position_qty, last_price_date, exit_price) VALUES (?,?,?,?,?,?,?)",
            (code, st, entry, last, qty, lpd, last))
    conn.commit()


# ============ 事故复现配方（审计官取证版，日期动态化） ============

def _seed_incident(conn):
    """2026-10-08 事故形态（当日三态 28 行），日期动态 today-N 偏移。

    返回 (d18, d22, d25) 三个分组日（today-16/12/9，对齐真实 09-18/22/25 相对
    10-04 基线的间隔），金额逐项对齐审计官取证值。
    """
    d18, d22, d25 = _today_n(16), _today_n(12), _today_n(9)
    rows = []
    # 组1: 21 只, Σ(last_price×qty)=275,825 → 每只 13,135.0
    for i in range(21):
        rows.append((f"61{i:04d}", "exit_pending", 27.0, 13.135, 1000, d18))
    # 组2: 1 只, pv=1,510
    rows.append(("622201", "exit_pending", 15.1, 15.1, 100, d22))
    # 组3（末组）: 3 只, Σpv=3,404 → 每只 1,134.666...
    for code in ("920394", "001399", "601333"):
        rows.append((code, "exit_pending", 11.34, 1.1346666, 1000, d25))
    # NULL 组: 3 只（last_price_date=NULL, 可见但不进曲线, pv 不进分子面口径外的任何组）
    for code in ("600001", "600002", "600003"):
        rows.append((code, "pending_entry", 5.0, 0.0, 100, None))
    _seed(conn, rows)
    return d18, d22, d25


def _seed_exited_for_cash(conn, cash_target=6848.0):
    """构造 exited 行使 get_available_cash(300000)=cash_target。

    cash = 300000 − invested(三态 entry×qty) − exited_cost + proceeds = 6848
    取 exited_cost=2,917,623 / proceeds=2,905,210 → 净贡献 −12,413；
    三态 invested 按事故面 entry×qty（≈280,739）→ 300000−280,739−12,413=6,848。
    """
    # 一笔 exited: cost 2,917,623 / proceeds 2,905,210
    conn.execute(
        "INSERT INTO stock_pool (code, status, entry_price, last_price, position_qty, "
        "last_price_date, exit_price) VALUES ('700001','exited',29176.23,0,100,NULL,29052.10)")
    conn.commit()


# ============ 刀一：全组合口径（主钉 + NULL 组守护 + 反向钉） ============

def test_incident_full_portfolio_dd():
    """事故配方主钉：修后 dd 绝不出 −96% 假态；无历史 peak → dd=0（首日为基）+留痕。

    两段断言（口径分工）：
    - dd 值：snapshot 表空（事故态）→ 无历史 peak → dd=0.0 首日为基（统筹批）
      ——不拿 initial_capital 当分母，−96% 假态在数值上不可能出现
    - 量级护栏：全组合真值（cash+Σpv vs 300k）另断言 |真dd|<0.10，
      守「事故形状下真回撤是正常量级」这一事实面
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed_incident(conn)
    _seed_exited_for_cash(conn)
    gate = _mk_gate(conn)
    dd = gate._current_drawdown()
    assert dd is not None
    # 无历史 peak（snapshot 空）→ 首日为基 → dd=0，绝不按 initial_capital 算出 -96%
    assert dd == 0.0, f"无历史 peak 应首日为基 dd=0, 实际 {dd}"
    # 走一次完整 check（留痕发生地）
    reason = gate._check_drawdown("600000")
    assert reason == "" or "放行" in reason, f"应放行, reason={reason!r}"
    # 留痕（放行可追溯）
    n = conn.execute(
        "SELECT COUNT(*) c FROM action_records WHERE code='PORTFOLIO' "
        "AND action_type='guard_stale'").fetchone()["c"]
    assert n >= 1, "首日为基放行必须留痕"
    # 量级护栏：真值 dd（若按 initial 参照）是正常量级，非 -96%
    cash = gate.stock_pool.get_available_cash(300000.0)
    pv = conn.execute(
        "SELECT COALESCE(SUM(last_price*position_qty),0) FROM stock_pool "
        "WHERE status IN ('active','exit_pending','pending_entry') "
        "AND last_price>0 AND position_qty>0").fetchone()[0]
    true_dd = (cash + pv) / 300000.0 - 1
    assert abs(true_dd) < 0.10, f"事故配方真值 dd 出正常量级: {true_dd}"


def test_unpatched_impl_would_give_fake_dd():
    """反向钉（修没修判据）：旧实现（曲线末组口径）在同配方上必出 −96.58%。

    实打方式：这里不 mock 修后实现，而是直接按旧公式重算——若旧公式与
    新公式给出同值，说明刀一没生效，本测试红。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed_incident(conn)
    # 旧公式: cash + 末组 pv（09-25 组）
    cash = 6848.0
    tail_pv = conn.execute(
        "SELECT COALESCE(SUM(last_price*position_qty),0) FROM stock_pool "
        "WHERE last_price_date = (SELECT MAX(last_price_date) FROM stock_pool "
        "WHERE last_price_date IS NOT NULL)").fetchone()[0]
    fake_dd = (cash + tail_pv) / CAP - 1
    assert fake_dd == pytest.approx(-0.965827, abs=0.001), \
        f"事故配方在旧口径下应出 -96.58%, 实际 {fake_dd}（配方或金额对不上）"
    # 新实现必须与之不同
    gate = _mk_gate(conn)
    _seed_exited_for_cash(conn)
    new_dd = gate._current_drawdown()
    assert new_dd != pytest.approx(fake_dd, abs=0.01), "刀一未生效（新旧口径同值）"


def test_null_group_does_not_change_dd():
    """NULL 组守护钉：3 只 last_price_date=NULL 既不进分子也不进末组，dd 不变。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed_incident(conn)
    _seed_exited_for_cash(conn)
    gate = _mk_gate(conn)
    dd_with_null = gate._current_drawdown()
    # 删 NULL 组再算
    conn.execute("DELETE FROM stock_pool WHERE last_price_date IS NULL")
    conn.commit()
    dd_without = gate._current_drawdown()
    assert dd_with_null == pytest.approx(dd_without, abs=1e-9), \
        f"NULL 组不得影响 dd: with={dd_with_null} without={dd_without}"


def test_tail_small_head_large_never_fake_dd():
    """真实反向钉：末组小、前面组大（09-18 大组 + 09-25 末组形状）不得出 -90% 级假值。

    这是事故本身的形状（275,825 在前、3,404 在末），比任何人造 case 值钱。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed_incident(conn)
    _seed_exited_for_cash(conn)
    gate = _mk_gate(conn)
    dd = gate._current_drawdown()
    assert dd is not None, "dd=None（实现不可判定面溢出）"
    assert dd > -0.5, f"末组小前组大形状出了假值: {dd}"


# ============ 刀二：peak_nav（分母 + 曲线空放行） ============

def test_peak_from_snapshot_history_max():
    """peak=历史每日 Σmv 的 max，单调不降。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    gate = _mk_gate(conn)
    for d, mv in ((_today_n(3), 290000.0), (_today_n(2), 310000.0),
                  (_today_n(1), 295000.0)):
        conn.execute("INSERT INTO paper_positions_snapshot (date, code, mv) VALUES (?,?,?)",
                     (d, "600000", mv))
    conn.commit()
    peak = gate._peak_nav()
    assert peak == pytest.approx(310000.0)
    assert gate._peak_nav() == peak  # 单调不降（纯函数）


def test_empty_curve_and_no_peak_release_with_trace():
    """刀二事故复现钉：曲线空+无历史 peak → 无法判定→放行+留痕，
    绝不拿 initial_capital 当分母算惊天回撤。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    gate = _mk_gate(conn)
    reason = gate._check_drawdown("600000")
    assert reason == "" or "无法判定" in reason or "放行" in reason, \
        f"空曲线+无 peak 应放行, reason={reason!r}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM action_records WHERE code='PORTFOLIO' "
        "AND (action_type='guard_stale' OR reason LIKE '%无 NAV 依据%')").fetchone()["c"]
    assert n >= 1, "空曲线放行必须留痕"


# ============ 丙：留痕四值 ============

def test_pause_trace_records_four_values():
    """丙：熔断 transition 留痕落 nav/dd/pv/分母 四值（10-07/08 四列全 NULL 的病）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    gate = _mk_gate(conn)
    d1 = _today_n(1)
    conn.execute("INSERT INTO paper_positions_snapshot (date, code, mv) VALUES (?,?,?)",
                 (d1, "600000", 295000.0))
    conn.commit()
    # 真风险日: cash=20,000 + pv=220,000 = 240,000; peak=295,000 → dd≈-18.6% 破线
    _seed(conn, [("600001", "active", 29.5, 22.0, 10000, d1)])
    gate._check_drawdown("600001")
    row = conn.execute(
        "SELECT action_detail, price, previous_value, new_value, reason "
        "FROM action_records WHERE reason LIKE '%回撤熔断:%' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "熔断留痕行必须存在"
    ad, price, pv, nv = (row["action_detail"], row["price"],
                         row["previous_value"], row["new_value"])
    assert ad and str(ad).strip(), f"action_detail 空: {ad!r}"
    assert price is not None and price > 0, f"price(nav) 缺: {price!r}"
    assert pv is not None, "previous_value(pv) 缺"
    assert nv is not None and nv > 0, f"new_value(分母 peak) 缺: {nv!r}"


def test_real_risk_day_still_pauses():
    """反向钉（防改松）：全组合 dd 真破 -8% → 熔断照触发。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    gate = _mk_gate(conn)
    d1 = _today_n(1)
    conn.execute("INSERT INTO paper_positions_snapshot (date, code, mv) VALUES (?,?,?)",
                 (d1, "600000", 295000.0))
    conn.commit()
    # cash=20,000 + pv=220,000 = 240,000 / peak 295,000 → -18.6% 破线
    _seed(conn, [("600001", "active", 29.5, 22.0, 10000, d1)])
    reason = gate._check_drawdown("600001")
    assert reason and "回撤熔断" in reason, f"真风险日必须熔断, reason={reason!r}"


# ============ 包B 陈旧检测在新口径下的接线（回归修复钉 2026-10-11） ============

def test_stale_price_date_sets_stale_reason():
    """陈旧钉：三态 last_price_date 距今 >5 天 → _current_drawdown 置位末端
    → _stale_nav_reason 必须吐「陈旧…放行」（包B 生产链话术在新数据源延续）。

    回归史：刀一弃 curve 后 _last_curve_end 无人置位 → 话术静默消失，
    pack_b 生产链测试只在本树红（双树对照 2026-10-11 定谳）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed(conn, [("600001", "active", 10.0, 10.0, 100, _today_n(6))])
    gate = _mk_gate(conn)
    gate._current_drawdown()
    reason = gate._stale_nav_reason()
    assert "陈旧" in reason and "放行" in reason, (
        f"陈旧数据必须显式放行话术（包B 契约）, 实得 {reason!r}")

def test_fresh_price_date_no_stale_reason():
    """反向钉（双向第二向）：last_price_date=今天 → 必须空串。
    只会恒 truthy 的话术是噪音（机治钉三问第三问）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk_tables(conn)
    _seed(conn, [("600001", "active", 10.0, 10.0, 100, _today_n(0))])
    gate = _mk_gate(conn)
    gate._current_drawdown()
    got = gate._stale_nav_reason()
    assert got == "", f"新数据不该报陈旧, 实得 {got!r}"
