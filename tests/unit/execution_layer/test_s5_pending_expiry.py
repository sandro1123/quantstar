"""S5: 过期 pending_entry 清理 — valid_until 已过不再每轮重评, 状态迁移留痕

生产实害 (2026-08-31): 4 笔 08-28 深夜挂入的 pending_entry valid_until=08-28
(写侧日历 bug, 见 test_calendar_forward.py), 之后每 5 分钟一轮触价检查都在
entry_engine 过期闸门被拒, 每轮刷一条 entry_blocks 行 (16→84, ~200 行/日推算),
今晨 002508 46 轮带内触价被拦 = 真实入场机会被吞。

迁移目标 = watch + cancel_entry 留痕 (S4 holding.py 先例: CHECK 白名单内,
语义诚实「入场计划作废回观察池」)。不新造 status 值 (#18 blocked 撞白名单前科)。
"""
import pytest

from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture()
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _make_pending(el, code, valid_until):
    el.stock_pool.add(code, f"测试{code}")
    el.stock_pool.update_status(code, "pending_entry")
    el.stock_pool.update(code, entry_low=10.0, entry_high=11.0,
                         valid_until=valid_until)


def test_stock_pool_has_expire_stale_pending(el):
    """stock_pool 应提供 expire_stale_pending (对齐 expire_stale_watch 先例)"""
    assert hasattr(el.stock_pool, "expire_stale_pending"),         "缺 expire_stale_pending 方法"


def test_expired_pending_migrates_to_watch_with_cancel_trace(el):
    """过期迁移: valid_until < today 的 pending → watch + cancel_entry 留痕

    S4 先例 (holding.py): pending_entry 作废 = update_status(watch) +
    action_records cancel_entry。白名单内、不伪装 exited。
    """
    _make_pending(el, "000001", "2020-01-01")
    expired = el.stock_pool.expire_stale_pending()
    assert "000001" in expired, "过期 pending 应被迁移"
    row = el.stock_pool.get("000001")
    assert row["status"] == "watch", "物理状态应回观察池 (CHECK 白名单内)"
    conn = el.db.get_connection()
    cancels = conn.execute(
        "SELECT reason FROM action_records "
        "WHERE code='000001' AND action_type='cancel_entry'"
    ).fetchall()
    assert cancels, "迁移应留 cancel_entry 痕"


def test_fresh_pending_not_migrated(el):
    """未过期 pending 不动 (valid_until=today 仍在窗内)"""
    _make_pending(el, "000002", "2099-01-01")
    expired = el.stock_pool.expire_stale_pending()
    assert "000002" not in expired
    assert el.stock_pool.get("000002")["status"] == "pending_entry"


def test_null_valid_until_not_touched(el):
    """valid_until 为空不迁移 (存量 S5 前语义, 宁可不误伤)"""
    _make_pending(el, "000003", None)
    expired = el.stock_pool.expire_stale_pending()
    assert "000003" not in expired
    assert el.stock_pool.get("000003")["status"] == "pending_entry"


def test_daily_update_wires_expiry(el):
    """铁律#11 接线守卫: daily_update 必须调用 expire_stale_pending (复用既有入口)"""
    import inspect
    from execution_layer import ExecutionLayer as EL
    src = inspect.getsource(EL.daily_update)
    assert "expire_stale_pending" in src,         "daily_update 应接线 pending 过期清理 (16:16 既有入口, 不新增 cron)"


def test_daily_update_migrates_expired_pending(el):
    """端到端: 过期 pending 经 daily_update 一次迁移, 不再每轮重评"""
    _make_pending(el, "002508", "2020-01-01")
    el.daily_update(klines=[])
    row = el.stock_pool.get("002508")
    assert row["status"] == "watch", "daily_update 应迁移过期 pending"
    conn = el.db.get_connection()
    cancels = conn.execute(
        "SELECT reason FROM action_records "
        "WHERE code='002508' AND action_type='cancel_entry'"
    ).fetchall()
    assert cancels, "应留 cancel_entry 痕"


def test_002508_regression_migrated_pending_not_reblocked(el):
    """002508 场景回归: 迁移后 pending 不再在触价检查中被过期闸门拦截

    生产实害链: valid_until=08-28 (挂入日) → 08-31 每 5 分钟一轮 check_entry
    → 过期闸门 skip_entry + entry_blocks 每轮刷行 (46 轮)。迁移后 stock 不再
    是 pending_entry, check_entry 不应再产生 valid_until 拦截留痕。
    """
    _make_pending(el, "002508", "2020-01-01")
    el.daily_update(klines=[])
    assert el.stock_pool.get("002508")["status"] == "watch"
    # 触价检查: watch 态 + 带内价不应再产生 valid_until 过期拦截
    decision = el.entry_engine.check_entry("002508", 15.95, "09:35")
    conn = el.db.get_connection()
    vu_blocks = conn.execute(
        "SELECT COUNT(*) AS c FROM entry_blocks WHERE code='002508' AND gate='valid_until'"
    ).fetchone()["c"]
    assert vu_blocks == 0, f"迁移后不应再有 valid_until 拦截行, got {vu_blocks}"
