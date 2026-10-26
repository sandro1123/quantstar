"""D席 fix/pool-position-guard — RED 先行（守卫测试）。

病理 (2026-09-16 定谳, 统筹 0270ea38 派单 + 审计官证据链):
  策略轮 add_to_watch 不检查目标是否已持仓 → 把「已持仓待结算」的股当新候选重新入池
  → update_status(code, 'watch'/'pending_entry') 覆写 → 了结字段被清空 + 状态机矛盾。
  实测: 688029 (watch/600股) / 003004 (pending_entry/600股)。

硬日期机制: expire_stale_watch (stock_pool.py:449) 判 status='watch' AND added_date < bj_date()-7
  → 688029 added_date=09-11 → 09-19 命中 → exited/watch_expired + EXIT_EVENT_FIELDS 全 NULL
  → 600 股以「观察到期」出场, exit_price=NULL → 真实成交损益永久丢失。

修法 (统筹裁定 ③⑤): 执行层唯一写入入口 update_status 内加公共守卫
  _guard_candidate_status_no_position: new_status ∈ {watch,pending_entry} 且 position_qty>0 → 拒写。
  留痕走 ops 域 AlertManager (warning), 不走 action_records (避 AUD-028 CHECK 面)。

判例遵守:
  - 夹具两表须同日 (防 date 落 now())  [N3 时点炸弹判例]
  - 验证首行打印「我在验什么」
  - 破坏性步骤前自检须是断言不是打印
"""
import pytest

from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    # alerts 表不在 db.init_db() 面 (ops_layer/core/tables.py:20 独立 DDL) —
    # 守卫留痕走 AlertManager, 测试须显式建表, 否则 "no such table: alerts"。
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _hold(exec_layer, code, qty=600, entry=72.29, status="active"):
    """造一只「已持仓」票：add → active → 建仓。"""
    sp = exec_layer.stock_pool
    sp.add(code, "测试票", "strategy", "建仓", 85)
    sp.update_status(code, "active")
    sp.update(code, position_qty=qty, entry_price=entry, entry_date="2026-09-11")
    return sp.get(code)


# ── RED-1: 守卫核心 —— 带仓票不得被打回 watch ─────────────────────────────
def test_guard_rejects_watch_with_position(exec_layer):
    """我在验：带仓 (position_qty>0) 的行, update_status('watch') 必须被拒。

    根因路径: 688029 的 status_change → watch (action_records id2529, source=execution)
    正是 update_status L149 写的。守卫必须在此拦下。
    """
    sp = exec_layer.stock_pool
    _hold(exec_layer, "688029", qty=600, entry=72.29, status="active")
    sp.update_status("688029", "watch")          # 病理调用
    row = sp.get("688029")
    assert row["status"] == "active", (
        f"带仓票被改写成 {row['status']} —— 守卫失效, 09-19 将以 watch_expired 丢单")


# ── RED-2: 第二条腿 —— 带仓票不得被打回 pending_entry ────────────────────
def test_guard_rejects_pending_entry_with_position(exec_layer):
    """我在验：带仓行的 update_status('pending_entry') 必须被拒。

    实测 003004: status=pending_entry / position_qty=600 / entry_price=65.25。
    统筹补充单: expire_stale_pending (WHERE status='pending_entry') 会吃它。
    """
    sp = exec_layer.stock_pool
    _hold(exec_layer, "003004", qty=600, entry=65.25, status="active")
    sp.update_status("003004", "pending_entry")
    row = sp.get("003004")
    assert row["status"] == "active", (
        f"带仓票被改写成 {row['status']} —— 第二条腿未覆盖")


# ── RED-3: 拒写必须显形 —— 走 AlertManager (统筹裁 ⑤, 铁律#12 降级不静默) ─
def test_guard_emits_alert(exec_layer):
    """我在验：拒写必须产生 AlertManager 告警 (warning 级), 不得只进日志。

    统筹裁: logger.warning 没人看=等于静默 → 走 ops 域 alert_manager。
    """
    sp = exec_layer.stock_pool
    _hold(exec_layer, "688029", qty=600, entry=72.29, status="active")
    sp.update_status("688029", "watch")
    conn = exec_layer.db.get_connection()
    rows = conn.execute(
        "SELECT title, detail, severity, layer FROM alerts "
        "WHERE title LIKE '%持仓%' OR title LIKE '%position%' "
        "OR detail LIKE '%688029%'").fetchall()
    assert len(rows) >= 1, "拒写未产生任何告警 —— 静默拒写 (违反铁律#12)"
    hit = [dict(r) for r in rows]
    assert any(r["severity"] in ("warning", "critical") for r in hit), \
        f"告警级别过低: {hit}"
    # detail 必须含判据要素 (统筹裁: code / 当前态 / position_qty / 被拒目标态)
    d = " ".join(str(r["detail"] or "") for r in hit)
    assert "688029" in d and "600" in d and "watch" in d, \
        f"告警 detail 缺判据要素 (code/qty/目标态): {d}"


# ── RED-4: 拒写后了结字段不得被动 ────────────────────────────────────────
def test_guard_preserves_exit_fields(exec_layer):
    """我在验：带仓票被拒写时, 其了结字段必须原样保留 (不得被 exit_fields_null_sql 清空)。

    这是本卡的损害面: 清空 → 真实成交损益永久丢失。
    造法: update_status('active') 是正门; 用直写模拟「上一世残留了结字段」的存量行
    (688029 实测: exit_price/exit_date 均在库) —— 直写仅用于**造夹具**,
    被测行为是 update_status 的守卫。
    """
    sp = exec_layer.stock_pool
    _hold(exec_layer, "688029", qty=600, entry=72.29, status="active")
    conn = exec_layer.db.get_connection()
    conn.execute("UPDATE stock_pool SET exit_price=?, exit_date=?, exit_reason=? "
                 "WHERE code=?", (75.10, "2026-09-11", "评分滑出候选名单", "688029"))
    conn.commit()
    before = sp.get("688029")
    assert before["exit_price"] == 75.10, "夹具造设失败: 了结字段未落库"
    sp.update_status("688029", "watch")
    after = sp.get("688029")
    assert after["exit_price"] == before["exit_price"], "了结价被清空"
    assert after["exit_date"] == before["exit_date"], "了结日被清空"
    assert after["exit_reason"] == before["exit_reason"], "了结原因被清空"


# ── RED-5: 零回归锚 —— 正常路径 (真·watch 票, 无仓) 逐字等价 ──────────────
def test_normal_path_unchanged(exec_layer):
    """我在验：真·watch 票 (position_qty 为 NULL/0) 的状态迁移必须零变化。

    不变量锚 (统筹单): 正常路径逐字等价。09-18 那 5 只到期票 qty=0 → 不受影响。
    """
    sp = exec_layer.stock_pool
    sp.add("300667", "真观察票", "strategy", "观察", 70)
    assert sp.get("300667")["status"] == "watch"
    sp.update_status("300667", "pending_entry")
    assert sp.get("300667")["status"] == "pending_entry"
    sp.update_status("300667", "watch")
    assert sp.get("300667")["status"] == "watch"
    # 建仓后 (qty>0) 转 active 是合法路径, 必须放行
    sp.update_status("300667", "active")
    sp.update("300667", position_qty=100)
    sp.update_status("300667", "exit_pending")   # active → exit_pending 合法
    assert sp.get("300667")["status"] == "exit_pending"
    sp.update_status("300667", "exited")          # 结算出场 合法
    assert sp.get("300667")["status"] == "exited"


# ── RED-6: 边界 —— exit_pending 带仓票不得被打回 watch (688332 对照组) ────
def test_guard_rejects_exit_pending_with_position(exec_layer):
    """我在验：exit_pending 态带仓票 (688332: qty=300) 被打回 watch 也必须拒。

    exit_pending = 待结算持仓, 其仓必须走结算出场, 不得被降级清空。
    """
    sp = exec_layer.stock_pool
    _hold(exec_layer, "688332", qty=300, entry=85.24, status="active")
    # 走真实正门: mark_exit_trigger 写 trigger_price/trigger_date (active→exit_pending)
    ok = sp.mark_exit_trigger("688332", 85.24, "评分滑出候选名单")
    assert ok is True, "夹具造设失败: mark_exit_trigger 未生效"
    assert sp.get("688332")["status"] == "exit_pending"
    sp.update_status("688332", "watch")           # 病理调用
    row = sp.get("688332")
    assert row["status"] == "exit_pending", (
        f"待结算带仓票被改写成 {row['status']} —— 结算链断裂")
