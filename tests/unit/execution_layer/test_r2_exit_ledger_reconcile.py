# -*- coding: utf-8 -*-
"""Round2 RED — 统计漏账对账（只报不改，统筹 sm-f8184cb7 裁 GO）。

病根（生产铁证，2026-09-15）:
    execution_stats 09-08 total_trades = 9
    stock_pool 里 exit_date='2026-09-08' 的行: status=exited 9 行 + **非 exited 1 行**
    save_daily 作用域 = "status='exited' AND exit_date=?"
→ 600152 那笔真实了结（09-07 买 / 09-08 T+1 卖 / +0.62%）因为行在 09-09 被打回 watch,
  **永远进不了统计面**。AUD-005 把真值回填进那一行了, 统计面却看不见它。

为什么是对账而不是改口径:
  `save_daily` 按**状态**圈作用域本身就脆 —— 行一流转, 历史交易就掉出统计
  (「按状态圈会漏, 按值形态/事件圈才稳」, 本判例第四次兑现)。但改 save_daily
  的口径会动既有统计语义, 影响面大, 属产品裁定。**本件只报不改**:
  把漏账显形, 补账与改口径各自走各自的决策 —— 同 `restore_coverage` 的路子。

只报不改是硬约束, 不是习惯: test_reconcile_is_read_only 用改前改后的全表快照锁死。
"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer

D = "2026-09-08"


@pytest.fixture
def layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _seed(pool, code, status, exit_date, ret):
    """造一行带了结账的票, status 由调用方指定（活态=漏账形态）。"""
    pool.add(code, code)
    conn = pool.db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET status=?, entry_price=10.0, exit_price=?, "
        "exit_date=?, exit_reason='T+1确认卖出', total_return_pct=? WHERE code=?",
        (status, 10.0 * (1 + ret / 100), exit_date, ret, code))
    conn.commit()


def _snapshot(conn):
    return (conn.execute("SELECT * FROM stock_pool ORDER BY code").fetchall(),
            conn.execute("SELECT * FROM execution_stats ORDER BY id").fetchall())


# ------------------------------------------------------------ 漏账检出

def test_orphan_exit_detected(layer):
    """exit_date 落在当日、status 却已流转走的行 = 掉出统计面的漏账。"""
    stats = layer.stats if hasattr(layer, "stats") else None
    from execution_layer.stats.performance import ExecutionStats
    st = stats or ExecutionStats(layer.db)
    pool = layer.stock_pool
    _seed(pool, "000001", "exited", D, 3.0)
    _seed(pool, "600152", "watch", D, 0.62)      # 生产原形: 被打回 watch

    rep = st.reconcile_exit_ledger(D)

    assert rep["counted"] == 1
    assert rep["orphaned"] == 1
    codes = [o["code"] for o in rep["orphans"]]
    assert codes == ["600152"]
    o = rep["orphans"][0]
    assert o["status"] == "watch"
    assert o["total_return_pct"] == 0.62, "漏掉的收益必须显形, 否则补账无从算起"


def test_clean_day_reports_zero_orphans(layer):
    from execution_layer.stats.performance import ExecutionStats
    st = ExecutionStats(layer.db)
    _seed(layer.stock_pool, "000001", "exited", D, 3.0)
    rep = st.reconcile_exit_ledger(D)
    assert rep["counted"] == 1 and rep["orphaned"] == 0 and rep["orphans"] == []


def test_counted_equals_save_daily_total_trades(layer):
    """对账的 counted 必须与 save_daily 真写进库的 total_trades 同源同值。

    否则对账本身就是第二套口径 —— 那是在漏账之上再加一层不一致。
    """
    from execution_layer.stats.performance import ExecutionStats
    st = ExecutionStats(layer.db)
    _seed(layer.stock_pool, "000001", "exited", D, 3.0)
    _seed(layer.stock_pool, "000002", "exited", D, -1.0)
    _seed(layer.stock_pool, "600152", "watch", D, 0.62)

    st.save_daily(D)
    written = layer.db.get_connection().execute(
        "SELECT total_trades FROM execution_stats "
        "WHERE period='daily' AND period_start=?", (D,)).fetchone()[0]

    assert st.reconcile_exit_ledger(D)["counted"] == written == 2


def test_census_over_all_dates(layer):
    """date=None = 全量普查 (统筹要的"出清单")。"""
    from execution_layer.stats.performance import ExecutionStats
    st = ExecutionStats(layer.db)
    _seed(layer.stock_pool, "000001", "exited", "2026-09-08", 3.0)
    _seed(layer.stock_pool, "600152", "watch", "2026-09-08", 0.62)
    _seed(layer.stock_pool, "300906", "pending_entry", "2026-09-07", -1.04)

    rep = st.reconcile_exit_ledger()

    assert rep["orphaned"] == 2
    assert set(rep["by_date"]) == {"2026-09-07", "2026-09-08"}
    assert rep["by_date"]["2026-09-07"]["orphaned"] == 1


def test_active_and_pending_also_count_as_orphans(layer):
    """漏账判据是「status != exited」, 不是「status == watch」——

    按具体状态枚举会随状态机演进漏掉新态; 按补集圈才稳
    (与 AUD-005 补充件"按值形态圈"同一条纪律的状态版)。
    """
    from execution_layer.stats.performance import ExecutionStats
    st = ExecutionStats(layer.db)
    for i, s in enumerate(("watch", "pending_entry", "active", "exit_pending")):
        _seed(layer.stock_pool, f"00000{i+1}", s, D, 1.0)
    assert st.reconcile_exit_ledger(D)["orphaned"] == 4


# ------------------------------------------------------------ 只报不改

def test_reconcile_is_read_only(layer):
    """**只报不改是硬约束**: 全表快照改前改后必须逐字节相同。"""
    from execution_layer.stats.performance import ExecutionStats
    st = ExecutionStats(layer.db)
    _seed(layer.stock_pool, "000001", "exited", D, 3.0)
    _seed(layer.stock_pool, "600152", "watch", D, 0.62)
    st.save_daily(D)
    conn = layer.db.get_connection()

    before = _snapshot(conn)
    st.reconcile_exit_ledger(D)
    st.reconcile_exit_ledger()
    after = _snapshot(conn)

    assert [tuple(r) for r in before[0]] == [tuple(r) for r in after[0]], "对账改了 stock_pool"
    assert [tuple(r) for r in before[1]] == [tuple(r) for r in after[1]], "对账改了 execution_stats"
