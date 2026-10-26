"""AUD-005 (P0) 回归: T+1 结算腿三重病根。

生产实录 2026-09-07/08: ddp 失败致 K 线部分落库, daily_update 16:16 结算时
EX-10 守卫查的是**全局** COUNT(daily_klines WHERE date=today) > 0 —— 别的股票有行
即放行; 但结算是**逐股**查价, 这 18 只查不到 -> 静默回落 item['last_price']
(存量值恰等于 entry_price) -> 写出 total_return_pct=0.00 的伪造账。
真值按当日 close 复算合计 +46.38%, 而 execution_stats 记成 25 连败零胜。
alerts 表全历史零结算类告警 -> 潜伏 4 个交易日无人知。

三条 RED 对应三重病根:
① 守卫口径与结算口径必须同源 (查本批 pending 的逐股覆盖, 非全局行数)
② 逐股查价落空**不得**回落 last_price 伪造收益
③ 顺延必须告警 (不能只进 logger/action_records)
"""
from common.database import Database
from common.utils import bj_date
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.stock_pool import StockPool
from execution_layer import ExecutionLayer


def _pool_with_pending(code="000001", entry_price=20.0, qty=100):
    """构造一只 exit_pending, 且 last_price == entry_price (生产 22 只的真实形态)。"""
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, entry_price, "
        "position_qty, stop_loss, holding_days, last_price) "
        "VALUES (?, ?, 'active', '2026-08-01', ?, ?, 10.0, 5, ?)",
        (code, "测试股", entry_price, qty, entry_price))
    conn.commit()
    pool.mark_exit_trigger(code, entry_price * 0.9, "止损")
    return db, pool


def _ensure_alerts_table(db):
    """生产库 alerts 表已存在 (105 行); :memory: 夹具需照真 DDL 建, 否则
    AlertManager.send 的 INSERT 走 fail-safe 分支只 warning, 断言面看不到告警。"""
    from ops_layer.core.tables import OPS_TABLES_DDL as TABLES_DDL
    conn = db.get_connection()
    for ddl in (TABLES_DDL if isinstance(TABLES_DDL, (list, tuple)) else [TABLES_DDL]):
        try:
            conn.executescript(ddl)
        except Exception:
            pass
    conn.commit()


def _make_layer(db, pool):
    _ensure_alerts_table(db)
    layer = ExecutionLayer.__new__(ExecutionLayer)
    layer.db = db
    layer.stock_pool = pool
    layer.action_record = ActionRecord(db)
    return layer


def _ensure_kline_table(db):
    db.get_connection().execute(
        "CREATE TABLE IF NOT EXISTS daily_klines "
        "(code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")


def _add_close(db, code, close, date=None):
    _ensure_kline_table(db)
    conn = db.get_connection()
    conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                 (code, date or bj_date(), close))
    conn.commit()


class TestPartialCoverageMustNotFabricate:
    """病根①+②: 全局守卫放行 + 逐股落空回落入场价 = 伪造 0% 收益。"""

    def test_other_stock_has_kline_but_pending_one_does_not(self):
        db, pool = _pool_with_pending("000001", entry_price=20.0)
        # 别的股票有当日 K 线 -> 全局 COUNT > 0 -> 旧守卫放行
        _add_close(db, "999999", 33.3)
        # 本批 pending 的 000001 当日无 K 线
        _make_layer(db, pool)._settle_pending_exits()

        row = pool.get("000001")
        assert row["status"] == "exit_pending", (
            "逐股 K 线缺失时必须顺延, 不得结算 (实得 status=%s, exit_price=%s, ret=%s)"
            % (row["status"], row["exit_price"], row["total_return_pct"]))
        assert row["exit_price"] is None, "未结算不得写 exit_price"
        assert row["total_return_pct"] is None, (
            "绝不得用存量 last_price(=入场价) 造出 0.00%% 收益 —— 这正是 18 笔伪造账的手")

    def test_no_exit_action_record_when_deferred(self):
        db, pool = _pool_with_pending("000001")
        _add_close(db, "999999", 33.3)
        _make_layer(db, pool)._settle_pending_exits()
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM action_records WHERE code='000001' AND action_type='exit'"
        ).fetchone()[0]
        assert n == 0, "顺延不得留下 exit 动作痕迹 (否则下游按已平仓处理)"


class TestDeferralMustAlert:
    """病根③: 顺延只进 logger/action_records, alerts 表零行 -> 潜伏 4 个交易日。"""

    def test_deferral_writes_alert_row(self):
        db, pool = _pool_with_pending("000001")
        _add_close(db, "999999", 33.3)
        _make_layer(db, pool)._settle_pending_exits()
        rows = db.get_connection().execute(
            "SELECT severity, layer, title, detail FROM alerts"
        ).fetchall()
        assert rows, "结算顺延必须发告警 (铁律 #12 可观测不可静默)"
        blob = " ".join(str(v) for r in rows for v in r)
        assert "000001" in blob or "结算" in blob or "顺延" in blob


class TestHappyPathPreserved:
    """负向对照: 本批全覆盖时照常结算, 修法不得破正常日。"""

    def test_full_coverage_still_settles_at_today_close(self):
        db, pool = _pool_with_pending("000001", entry_price=20.0, qty=100)
        _add_close(db, "000001", 18.0)
        _make_layer(db, pool)._settle_pending_exits()
        row = pool.get("000001")
        assert row["status"] == "exited"
        assert row["exit_price"] == 18.0
        assert row["total_return_pct"] == -10.0        # (18-20)/20*100
        assert row["total_fees"] > 0
