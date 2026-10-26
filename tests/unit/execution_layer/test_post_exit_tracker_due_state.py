"""D席 fix/post-exit-due-logging — RED 先行（静默跳过三态留痕）。

病理 (2026-09-16 定谳, 统筹 0270ea38 派单 + D席观察项读数卡):
  post_exit_tracker._due_point_price 的
      if len(rows) < need: return 0   # 尚未过 N 个交易日, 未到期
  把**两种语义吞成同一个 0**:
    ① 真·未到期 (时间没到)            → 应静默
    ② 已到期但取不到数据 (K线断供/停牌) → 必须留痕
  实案 9 条: exit_date=2026-09-08 的 T+5, 市场日历已过 5 个交易日 (09-15),
  但个股当日 K 线缺行 (仅 4 天) → 误判「未到期」→ 静默跳过
  → 当天该做的检查被吞, 且只有事后补算才看得出。

修法 (统筹裁): 用**全市场交易日历**判「是否到期」(日历概念: 市场开市即过一天),
  个股 K 线只负责「取价」—— 把「到期判定」与「取价」解耦。
  三态: NOT_DUE / DUE_DONE / DUE_SKIPPED_NO_DATA。
  check() 返回 int 契约不变; 另加 last_check_stats 承载三态计数。
  **不改原到期判定语义** (个股 K 线算 due 的原路径保留), 只加全市场日历辅助判别。

判例遵守: 验证首行打印「我在验什么」/ 夹具两表须同日 / 破坏性步骤前自检须是断言。
"""
import pytest

from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    # alerts 表不在 db.init_db() 面 (ops_layer/core/tables.py 的 OPS_TABLES_DDL)
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _seed(exec_layer, code, dates_closes):
    conn = exec_layer.db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines "
                 "(code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
    for d, c in dates_closes:
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) "
                     "VALUES (?, ?, ?)", (code, d, c))
    conn.commit()


def _track(exec_layer, code, exit_date, exit_price=15.0):
    exec_layer.post_exit_tracker.create_tracking(
        code=code, exit_date=exit_date, exit_price=exit_price,
        entry_price=10.0, total_return_pct=50.0, exit_reason="止损")


# ── RED-1: 核心 —— 市场已到期但个股无数据, 必须报 DUE_SKIPPED_NO_DATA ──────
def test_due_skipped_no_data_detected(exec_layer):
    """我在验：市场日历已过 N 交易日、但个股 K 线不足时, 必须判 DUE_SKIPPED_NO_DATA。

    这是 9 条实案的同形复刻: exit_date=08-01, 个股仅 4 天 (T+5 需要 5),
    但市场腿有 6 天 → 市场已到期 → 不得误判「未到期」静默跳过。
    """
    _track(exec_layer, "000001", "2026-08-01")
    # 目标股: 退出后仅 4 个交易日 (不足 T+5)
    _seed(exec_layer, "000001", [
        ("2026-08-04", 14.5), ("2026-08-05", 14.0),
        ("2026-08-06", 13.8), ("2026-08-07", 13.5)])
    # 市场腿: 覆盖 6 个交易日 (构成全市场日历)
    _seed(exec_layer, "000002", [
        ("2026-08-04", 1.0), ("2026-08-05", 1.0), ("2026-08-06", 1.0),
        ("2026-08-07", 1.0), ("2026-08-08", 1.0), ("2026-08-11", 1.0)])
    exec_layer.post_exit_tracker.check()
    st = exec_layer.post_exit_tracker.last_check_stats
    assert st["DUE_SKIPPED_NO_DATA"] >= 1, (
        f"市场已到期但个股无数据被静默吞掉 (统计={st}) —— 9 条实案同形")


# ── RED-2: 跳过必须留痕 (告警面, 铁律#12) ────────────────────────────────
def test_skipped_emits_alert(exec_layer):
    """我在验：DUE_SKIPPED_NO_DATA 必须产生 AlertManager 告警 (不得只进日志)。"""
    _track(exec_layer, "000001", "2026-08-01")
    _seed(exec_layer, "000001", [
        ("2026-08-04", 14.5), ("2026-08-05", 14.0),
        ("2026-08-06", 13.8), ("2026-08-07", 13.5)])
    _seed(exec_layer, "000002", [
        ("2026-08-04", 1.0), ("2026-08-05", 1.0), ("2026-08-06", 1.0),
        ("2026-08-07", 1.0), ("2026-08-08", 1.0), ("2026-08-11", 1.0)])
    exec_layer.post_exit_tracker.check()
    conn = exec_layer.db.get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT title, detail, severity, layer FROM alerts "
        "WHERE title LIKE '%退出后%' OR title LIKE '%tracking%' "
        "OR title LIKE '%检查点%' OR detail LIKE '%000001%'").fetchall()]
    assert len(rows) >= 1, "跳过未产生任何告警 —— 静默跳过 (违反铁律#12)"
    d = " ".join(str(r["detail"] or "") for r in rows)
    assert "000001" in d and "T+5" in d, f"告警 detail 缺判据要素: {d}"


# ── RED-3: 真·未到期 必须静默 (不得误报) ─────────────────────────────────
def test_not_due_is_silent(exec_layer):
    """我在验：真·未到期 (市场日历也没过 N 日) 必须静默 —— 零告警、零跳过计数。

    这是「误报」防线: 若把未到期也报 DUE_SKIPPED_NO_DATA, 会天天刷屏。
    """
    _track(exec_layer, "000001", "2026-08-01")
    _seed(exec_layer, "000001", [("2026-08-04", 14.5), ("2026-08-05", 14.0)])
    _seed(exec_layer, "000002", [("2026-08-04", 1.0), ("2026-08-05", 1.0)])
    exec_layer.post_exit_tracker.check()
    st = exec_layer.post_exit_tracker.last_check_stats
    assert st["NOT_DUE"] >= 1, f"未到期未被识别为 NOT_DUE: {st}"
    assert st["DUE_SKIPPED_NO_DATA"] == 0, (
        f"未到期被误报为跳过 (会天天刷屏): {st}")
    conn = exec_layer.db.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM alerts WHERE detail LIKE '%000001%'").fetchone()[0]
    assert n == 0, f"未到期产生了告警 (误报): {n} 条"


# ── RED-4: 正常到期 必须照常评估 (DUE_DONE) ──────────────────────────────
def test_due_done_normal(exec_layer):
    """我在验：到期且取到价时, 照常评估 quality 且计入 DUE_DONE。"""
    _track(exec_layer, "000001", "2026-08-01")
    _seed(exec_layer, "000001", [
        ("2026-08-04", 15.5), ("2026-08-05", 16.0), ("2026-08-06", 16.5),
        ("2026-08-07", 17.0), ("2026-08-08", 17.5)])
    n = exec_layer.post_exit_tracker.check()
    assert n == 1, f"T+5 应被评估: {n}"
    st = exec_layer.post_exit_tracker.last_check_stats
    assert st["DUE_DONE"] == 1, f"DUE_DONE 计数错: {st}"
    recs = {r["check_point"]: r for r in exec_layer.post_exit_tracker.get_tracking("000001")}
    assert recs["T+5"]["quality"] == "wrong", "正常评估路径被破坏"


# ── RED-5: 跳过与完成必须可区分 (统筹要求 b) ─────────────────────────────
def test_skipped_distinguishable_from_done(exec_layer):
    """我在验：同一次 check() 里, DUE_DONE 与 DUE_SKIPPED_NO_DATA 同时可辨。

    造态: 两只票同 exit_date, 一只 K 线齐 (DONE), 一只缺 (SKIPPED)。
    """
    _track(exec_layer, "000001", "2026-08-01")
    _track(exec_layer, "000003", "2026-08-01")
    _seed(exec_layer, "000001", [
        ("2026-08-04", 15.5), ("2026-08-05", 16.0), ("2026-08-06", 16.5),
        ("2026-08-07", 17.0), ("2026-08-08", 17.5)])          # 齐 → DONE
    _seed(exec_layer, "000003", [("2026-08-04", 15.5)])        # 缺 → SKIPPED
    _seed(exec_layer, "000002", [
        ("2026-08-04", 1.0), ("2026-08-05", 1.0), ("2026-08-06", 1.0),
        ("2026-08-07", 1.0), ("2026-08-08", 1.0)])             # 市场日历
    exec_layer.post_exit_tracker.check()
    st = exec_layer.post_exit_tracker.last_check_stats
    assert st["DUE_DONE"] == 1, f"DUE_DONE 应为 1: {st}"
    assert st["DUE_SKIPPED_NO_DATA"] == 1, f"DUE_SKIPPED_NO_DATA 应为 1: {st}"


# ── RED-6: check() 返回 int 契约不变 ─────────────────────────────────────
def test_check_returns_int_unchanged(exec_layer):
    """我在验：check() 仍返回 int (已评估条数), 不因三态改造而改类型。"""
    _track(exec_layer, "000001", "2026-08-01")
    _seed(exec_layer, "000001", [
        ("2026-08-04", 15.5), ("2026-08-05", 16.0), ("2026-08-06", 16.5),
        ("2026-08-07", 17.0), ("2026-08-08", 17.5)])
    n = exec_layer.post_exit_tracker.check()
    assert isinstance(n, int) and not isinstance(n, bool), f"返回类型变: {type(n)}"
    # 显式 klines_by_code 路径也返回 int
    _track(exec_layer, "000004", "2026-08-01")
    n2 = exec_layer.post_exit_tracker.check({"000004": [type("K", (), {"close": 13.0})()]})
    assert isinstance(n2, int) and n2 == 3, f"显式路径返回异常: {n2}"


# ── RED-7: 零回归锚 (先行绿) —— 真·watch 无仓票语义不变 ─────────────────
def test_normal_path_unchanged_anchor(exec_layer):
    """我在验：既有正常语义零变化 (不传 klines 时自查 daily_klines 到期评估)。

    对齐既有 test_check_due_point_from_daily_klines: 退出后 6 个交易日
    → T+5 到期评估, T+10/T+20 未到期。
    """
    _track(exec_layer, "000001", "2026-08-01")
    _seed(exec_layer, "000001", [
        ("2026-08-04", 14.5), ("2026-08-05", 14.0), ("2026-08-06", 13.8),
        ("2026-08-07", 13.5), ("2026-08-08", 13.0), ("2026-08-11", 12.8)])
    checked = exec_layer.post_exit_tracker.check()
    assert checked == 1, f"T+5 应被评估 (既有语义): {checked}"
    recs = {r["check_point"]: r for r in exec_layer.post_exit_tracker.get_tracking("000001")}
    assert recs["T+5"]["quality"] == "correct"
    assert recs["T+5"]["current_price"] == 13.0
    assert recs["T+10"]["quality"] is None
    assert recs["T+20"]["quality"] is None
