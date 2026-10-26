# -*- coding: utf-8 -*-
"""AUD-027 RED: 状态流转离开了结态时, 了结事件字段必须清干净。

生产实证 (2026-09-14 取证, 两条后果都已实锤):
  600152 — T+1 伪造结算写入 exit_price=8.13(=entry_price)/total_return_pct=0.0,
           09-09 收到 sell_signal 被打回 watch, **exit_* 一个没清**。
  300906 — 同样被打回 watch 后, 今日 08:05 被 expire_stale_watch 二次退出:
           exit_reason 覆盖成 'watch_expired'、exit_date 覆盖成今日,
           而 exit_price=29.43 / total_return_pct=0.0 **原封不动继承下来** ——
           一次**观察到期**竟带着卖出价和收益进了统计面。

两条后果:
  ① 让按「状态」圈的回填作用域变盲 (AUD-005 补充件的 2 只就是被这个甩出去的)
  ② 二次退出覆盖首次了结日 —— 同一笔了结, stock_pool 说 09-14,
     post_exit_tracking 说 09-07, 真实了结日已被改写

修法口径: 行重新「活着」(watch/pending_entry/active) 时, 一切描述**上一次了结事件**
的字段都是残留, 必须清。计数类 (partial_exit_count/swap_count/add_position_count/
total_fees) 是设计上的累计历史, **不清** —— 那是 AUD-007 建立的语义。
"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer

# 与 src 侧常量对拍用 (故意在测试里独立写一份 —— 同一份常量自己跟自己比恒真, 测不出东西)
EXIT_EVENT_FIELDS = (
    "exit_reason", "exit_reason_detail", "exit_layer", "exit_date", "exit_source",
    "exit_price", "exit_low", "exit_high", "exit_plan_date",
    "trigger_price", "trigger_date",
    "total_return", "total_return_pct", "net_return", "net_return_pct",
)
# 累计历史, 复活时**不该**被清
CUMULATIVE_FIELDS = ("partial_exit_count", "swap_count", "total_fees")


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _seed_settled(pool, code="600152", entry=8.13):
    """造一行与生产 600152 同形的「已了结」行 (伪造 0% 那一笔), 并带上累计计数。"""
    pool.add(code, "测试股")
    conn = pool.db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET status='exited', entry_price=?, position_qty=0, "
        "exit_price=?, exit_date='2026-09-08', exit_reason='T+1 结算卖出', "
        "exit_reason_detail='T+1 收盘价结算', exit_layer=4, exit_source='settlement', "
        "exit_low=8.00, exit_high=8.30, exit_plan_date='2026-09-07', "
        "trigger_price=8.13, trigger_date='2026-09-07', "
        "total_return=0.0, total_return_pct=0.0, net_return=-12.0, net_return_pct=-0.15, "
        "partial_exit_count=2, swap_count=1, total_fees=35.5 "
        "WHERE code=?", (entry, entry, code))
    conn.commit()
    return code


def _dirty_fields(row):
    return sorted(f for f in EXIT_EVENT_FIELDS if row[f] is not None)


# ---------------------------------------------------------------- 复活面 (正面)

@pytest.mark.parametrize("alive_status", ["watch", "pending_entry", "active"])
def test_revival_clears_all_exit_event_fields(exec_layer, alive_status):
    """exited → 活态: 15 个了结事件字段必须全部归 NULL。

    生产病灶: 600152 走 exited → pending_entry → watch, 全程无人清。
    """
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    pool.update_status(code, alive_status)

    row = pool.get(code)
    assert row["status"] == alive_status
    assert _dirty_fields(row) == [], (
        f"→{alive_status} 后仍残留上一世了结字段: {_dirty_fields(row)}")


def test_revival_keeps_cumulative_counters(exec_layer):
    """计数/费用是累计历史, 复活**不该**清 —— 别把 AUD-007 的语义拆了。"""
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    pool.update_status(code, "watch")

    row = pool.get(code)
    assert row["partial_exit_count"] == 2
    assert row["swap_count"] == 1
    assert row["total_fees"] == 35.5


def test_revival_then_reexit_does_not_inherit_old_exit_date(exec_layer):
    """300906 的真实剧本: 了结 → 打回 watch → 二次退出。

    清干净后, 二次退出写的是它自己的日期, 不会与首次了结日混成一笔。
    """
    pool = exec_layer.stock_pool
    code = _seed_settled(pool, code="300906", entry=29.43)
    pool.update_status(code, "watch")
    mid = pool.get(code)
    assert mid["exit_date"] is None, "打回 watch 时首次了结日就该清掉"
    assert mid["exit_price"] is None


# ---------------------------------------------------------------- 了结面 (反面对照)

def test_transition_to_exited_preserves_exit_fields(exec_layer):
    """反面对照: →exited 是进入了结态, 一个字段都不许动。"""
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    pool.update_status(code, "active")          # 先清干净
    conn = pool.db.get_connection()
    conn.execute("UPDATE stock_pool SET exit_price=9.9, exit_reason='止损' WHERE code=?", (code,))
    conn.commit()
    pool.update_status(code, "exited")

    row = pool.get(code)
    assert row["exit_price"] == 9.9
    assert row["exit_reason"] == "止损"


def test_transition_to_exit_pending_preserves_trigger(exec_layer):
    """反面对照: exit_pending 是了结中间态, mark_exit_trigger 刚写的触发信息不许被清。"""
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    pool.update_status(code, "active")
    pool.mark_exit_trigger(code, 8.50, "跌破止损", exit_low=8.40)
    pool.update_status(code, "exit_pending")

    row = pool.get(code)
    assert row["trigger_price"] == 8.50
    assert row["exit_reason"] == "跌破止损"


# ---------------------------------------------------------------- 幽灵卖出价

def test_watch_expiry_carries_no_phantom_exit_price(exec_layer):
    """观察到期**不是一笔交易**, 不该有卖出价/收益。

    生产病灶: expire_stale_watch 只写 exit_reason/exit_date/exit_source,
    不碰 exit_price/total_return_pct —— 300906 就这样带着 29.43 和 0.00%
    以 'watch_expired' 身份进了 execution_stats。
    """
    pool = exec_layer.stock_pool
    code = _seed_settled(pool, code="300906", entry=29.43)
    conn = pool.db.get_connection()
    # 直写 watch: 模拟「历史脏行」(update_status 修好前入库的存量), 绕过清洗
    conn.execute("UPDATE stock_pool SET status='watch', added_date='2020-01-01' WHERE code=?", (code,))
    conn.commit()

    expired = pool.expire_stale_watch(expire_days=7)
    assert code in expired

    row = pool.get(code)
    assert row["status"] == "exited"
    assert row["exit_reason"] == "watch_expired"
    assert row["exit_price"] is None, "观察到期竟带着卖出价 —— 幽灵交易"
    assert row["total_return_pct"] is None, "观察到期竟带着收益率 —— 幽灵交易"
    assert row["trigger_price"] is None
    assert row["net_return_pct"] is None


# ---------------------------------------------------------------- 留痕面

def test_clearing_is_traced_not_silent(exec_layer):
    """清字段是有损动作, 必须留痕 —— 否则就是换个姿势的静默覆盖。"""
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    pool.update_status(code, "watch")

    conn = pool.db.get_connection()
    rows = conn.execute(
        "SELECT reason, action_detail FROM action_records "
        "WHERE code=? AND action_type='status_change' ORDER BY id DESC LIMIT 1",
        (code,)).fetchall()
    assert rows, "状态变更没留痕"
    blob = " ".join(str(x) for x in rows[0])
    assert "cleared_exit_fields=" in blob, f"清字段未留痕: {blob}"
    assert "cleared_exit_fields=0" not in blob, "明明清掉了却记成 0"


def test_clean_row_transition_records_no_clearing(exec_layer):
    """干净行走同一条路不该谎报「清过」—— 留痕要诚实, 不是盖章。"""
    pool = exec_layer.stock_pool
    pool.add("000001", "平安银行")
    pool.update_status("000001", "pending_entry")

    conn = pool.db.get_connection()
    row = conn.execute(
        "SELECT reason, action_detail FROM action_records "
        "WHERE code='000001' AND action_type='status_change' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    blob = " ".join(str(x) for x in row)
    assert "cleared_exit_fields=" not in blob or "cleared_exit_fields=0" in blob


# ---------------------------------------------------------------- 绕行路径 (advisor 直写 SQL)

def test_advisor_downgrade_to_watch_clears(exec_layer):
    """绕行路径①: recommendation_advisor 条件单到期降级 (直写 status='watch')。

    与 AUD-026「在册记双入口、实测三入口」同构 —— 只修正门会漏。
    """
    from execution_layer.advisor import recommendation_advisor as ra
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    conn = pool.db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET status='pending_entry', valid_until='2026-09-01' "
        "WHERE code=?", (code,))
    conn.commit()

    ra.pool_maintenance(pool.db, today="2026-09-14")

    row = pool.get(code)
    assert row["status"] == "watch"
    assert _dirty_fields(row) == [], f"降级回 watch 后仍残留: {_dirty_fields(row)}"


def test_advisor_promote_to_pending_entry_clears(exec_layer):
    """绕行路径②: recommendation_advisor 池内升推荐 (直写 status='pending_entry')。"""
    from execution_layer.advisor import recommendation_advisor as ra
    pool = exec_layer.stock_pool
    code = _seed_settled(pool)
    conn = pool.db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET status='watch', current_score=88 WHERE code=?", (code,))
    conn.commit()

    ra.promote_recommendations(pool.db, [], n_target=5, today="2026-09-14")

    row = pool.get(code)
    assert row["status"] == "pending_entry"
    assert _dirty_fields(row) == [], f"升推荐后仍残留: {_dirty_fields(row)}"


# ---------------------------------------------------------------- 单一定义点

def test_src_field_list_matches_schema(exec_layer):
    """守卫: src 侧字段清单必须与本测试的独立清单一致, 且全部真实存在于表。

    防的是「新增了结字段忘了登记」—— 与 AUD-008 白名单同族病。
    """
    from execution_layer.core.stock_pool import EXIT_EVENT_FIELDS as SRC_FIELDS
    assert sorted(SRC_FIELDS) == sorted(EXIT_EVENT_FIELDS)

    cols = {r[1] for r in
            exec_layer.stock_pool.db.get_connection().execute("PRAGMA table_info(stock_pool)")}
    missing = [f for f in SRC_FIELDS if f not in cols]
    assert missing == [], f"清单里有表中不存在的列: {missing}"
