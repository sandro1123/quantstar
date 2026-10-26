"""F4.4 退出结算收敛回归测试 — 触发→结算两段分离, 无双结算"""
from unittest.mock import MagicMock

from common.database import Database
from common.utils import bj_date
from execution_layer.core.stock_pool import StockPool


def _make_pool_with_active(code="000001", status="active", entry_price=20.0, qty=100,
                           stop_loss=10.0, last_price=15.0):
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    conn = db.get_connection()
    conn.execute("""
        INSERT INTO stock_pool (code, name, status, added_date, entry_price, position_qty,
                                stop_loss, holding_days, last_price)
        VALUES (?, ?, ?, '2026-08-01', ?, ?, ?, 5, ?)
    """, (code, "测试股", status, entry_price, qty, stop_loss, last_price))
    conn.commit()
    return db, pool


class TestMarkExitTrigger:
    def test_active_to_exit_pending(self):
        db, pool = _make_pool_with_active()
        ok = pool.mark_exit_trigger("000001", 9.0, "止损", exit_low=8.8)
        assert ok is True
        row = pool.get("000001")
        assert row["status"] == "exit_pending"
        assert row["trigger_price"] == 9.0
        assert row["trigger_date"] == bj_date()
        assert row["exit_reason"] == "止损"
        assert row["exit_low"] == 8.8

    def test_idempotent_second_call_skipped(self):
        db, pool = _make_pool_with_active()
        assert pool.mark_exit_trigger("000001", 9.0, "止损") is True
        # 二次触发: 已是 exit_pending, 不覆盖首次 trigger_price
        assert pool.mark_exit_trigger("000001", 8.0, "重复触发") is False
        row = pool.get("000001")
        assert row["status"] == "exit_pending"
        assert row["trigger_price"] == 9.0
        assert row["exit_reason"] == "止损"

    def test_rejects_non_active(self):
        db, pool = _make_pool_with_active(status="pending_entry")
        assert pool.mark_exit_trigger("000001", 9.0, "止损") is False
        assert pool.get("000001")["status"] == "pending_entry"

    def test_writes_action_record(self):
        db, pool = _make_pool_with_active()
        pool.mark_exit_trigger("000001", 9.0, "止损")
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='exit_check'"
        ).fetchall()
        assert len(rows) == 1


class TestIntradayScanMarksOnly:
    def _make_monitor(self, db, pool, trigger_result):
        from execution_layer.intraday.monitor import IntradayMonitor
        monitor = IntradayMonitor(db, pool, entry_engine=None, exit_engine=MagicMock())
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = trigger_result
        monitor.paper_broker = MagicMock()
        return monitor

    def test_scan_marks_trigger_does_not_settle(self):
        db, pool = _make_pool_with_active()
        monitor = self._make_monitor(db, pool, {"price": 9.0, "reason": "止损", "triggered": True})
        monitor._scan()
        row = pool.get("000001")
        # 只标记: exit_pending, 未结算为 exited
        assert row["status"] == "exit_pending"
        assert row["trigger_price"] == 9.0
        assert row["exit_price"] is None
        # 盘中绝不执行结算
        monitor.paper_broker.execute_exit.assert_not_called()

    def test_scan_no_trigger_keeps_active(self):
        db, pool = _make_pool_with_active()
        monitor = self._make_monitor(db, pool, None)
        monitor._scan()
        assert pool.get("000001")["status"] == "active"

    def test_post_close_marks_trigger_does_not_settle(self):
        db, pool = _make_pool_with_active()
        monitor = self._make_monitor(db, pool, {"price": 9.0, "reason": "止损", "triggered": True})
        monitor._post_close_processing()
        row = pool.get("000001")
        assert row["status"] == "exit_pending"
        monitor.paper_broker.execute_exit.assert_not_called()


class TestSettlePendingExitsSoleEntry:
    def _make_layer(self, db, pool):
        from execution_layer import ExecutionLayer
        from execution_layer.core.action_record import ActionRecord
        layer = ExecutionLayer.__new__(ExecutionLayer)
        layer.db = db
        layer.stock_pool = pool
        layer.action_record = ActionRecord(db)
        return layer

    def _add_today_close(self, db, code, close):
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     (code, bj_date(), close))
        conn.commit()

    def test_settle_uses_today_close_with_fees(self):
        db, pool = _make_pool_with_active(entry_price=20.0, qty=100)
        pool.mark_exit_trigger("000001", 9.0, "止损")
        self._add_today_close(db, "000001", 18.0)
        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()
        row = pool.get("000001")
        assert row["status"] == "exited"
        assert row["exit_price"] == 18.0
        assert row["exit_date"] == bj_date()
        assert row["total_fees"] > 0
        # 净收益 = 卖出净额 - 入场成本
        assert row["net_return"] == round(100 * 18.0 - row["total_fees"] - 100 * 20.0, 2)
        assert row["net_return_pct"] < row["total_return_pct"]  # 扣费后更低

    def test_no_double_settlement(self):
        db, pool = _make_pool_with_active()
        pool.mark_exit_trigger("000001", 9.0, "止损")
        self._add_today_close(db, "000001", 18.0)
        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()
        layer._settle_pending_exits()  # 二次: 无 exit_pending, 空转
        conn = db.get_connection()
        exits = conn.execute(
            "SELECT COUNT(*) FROM action_records WHERE code='000001' AND action_type='exit'"
        ).fetchone()[0]
        assert exits == 1
        assert pool.get("000001")["status"] == "exited"

    def test_skip_without_today_close(self):
        db, pool = _make_pool_with_active(last_price=0)
        pool.mark_exit_trigger("000001", 9.0, "止损")
        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()  # 无当日收盘价且无存量价 → 跳过, 保持 exit_pending
        assert pool.get("000001")["status"] == "exit_pending"

    def test_trigger_then_settle_full_separation(self):
        """端到端: 盘中触发标记 → 状态分离 → T+1 结算唯一入口完成退出"""
        db, pool = _make_pool_with_active()
        # 阶段1: 盘中扫描只标记
        from execution_layer.intraday.monitor import IntradayMonitor
        monitor = IntradayMonitor(db, pool, entry_engine=None, exit_engine=MagicMock())
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = {"price": 9.0, "reason": "止损", "triggered": True}
        monitor.paper_broker = MagicMock()
        monitor._scan()
        assert pool.get("000001")["status"] == "exit_pending"
        monitor.paper_broker.execute_exit.assert_not_called()
        # 阶段2: 次日 daily_update 结算
        self._add_today_close(db, "000001", 18.0)
        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()
        row = pool.get("000001")
        assert row["status"] == "exited"
        assert row["exit_price"] == 18.0




class TestEX10SettlementTimingAndExitSource:
    def _make_layer(self, db, pool):
        from execution_layer import ExecutionLayer
        from execution_layer.core.action_record import ActionRecord
        layer = ExecutionLayer.__new__(ExecutionLayer)
        layer.db = db
        layer.stock_pool = pool
        layer.action_record = ActionRecord(db)
        return layer

    def test_settle_delayed_when_kline_missing(self):
        """EX-10 (a): 本批 pending 的当日收盘价未覆盖 → 顺延, 保持 exit_pending, 写 skip_exit。

        AUD-005 (P0) 口径变更: 守卫由「全局 COUNT(daily_klines WHERE date=today)」
        改为「本批 pending 逐股覆盖」——旧口径与结算的逐股查价不同源, 部分落库时
        放行后逐股落空, 掉进回落 entry_price 的伪造面 (18 笔实证)。
        行为契约不变 (顺延/保持 exit_pending/不写 exit_price/一条 skip_exit),
        仅顺延文案随口径改准: 旧文案「当日 K 线尚未入库」在部分落库时是误导
        (全局 27,000 行在库而本批 0 行), 而误导的心智模型正是本 P0 的成因。
        """
        db, pool = _make_pool_with_active(entry_price=20.0, qty=100)
        pool.mark_exit_trigger("000001", 9.0, "止损")
        # Ensure daily_klines exists but has 0 rows for today
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.commit()

        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()

        # 验证: 保持 exit_pending (顺延不结算)
        row = pool.get("000001")
        assert row["status"] == "exit_pending"
        assert row["exit_price"] is None

        # 验证: action_records 写入 skip_exit 告警
        records = conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='skip_exit'"
        ).fetchall()
        assert len(records) == 1
        reason = records[0]["reason"]
        assert "结算顺延" in reason
        assert "未覆盖" in reason, f"顺延文案须点明覆盖缺口, 实得: {reason}"

    def test_settle_proceeds_when_kline_present(self):
        """EX-10 (a): 当日 K 线已入库 (count>0) → 正常结算 exit_pending → exited"""
        db, pool = _make_pool_with_active(entry_price=20.0, qty=100)
        pool.mark_exit_trigger("000001", 9.0, "止损")
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", bj_date(), 18.0))
        conn.commit()

        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()

        row = pool.get("000001")
        assert row["status"] == "exited"
        assert row["exit_price"] == 18.0

    def test_exit_source_cleanup_and_engine_distinction(self, monkeypatch):
        """EX-10 (b): track_exit_source 开启时区分 cleanup 与 engine; 默认关闭时不填入"""
        db, pool = _make_pool_with_active(entry_price=20.0, qty=100)
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", bj_date(), 18.0))
        conn.commit()

        # 默认关闭 track_exit_source: engine 结算 exit_source 保持 NULL
        pool.mark_exit_trigger("000001", 9.0, "止损")
        layer = self._make_layer(db, pool)
        layer._settle_pending_exits()
        assert pool.get("000001")["exit_source"] is None

        # 开启 track_exit_source
        monkeypatch.setattr("config_layer.cfg_get", lambda k, d=None: True if k == "execution.track_exit_source" else d)

        # 1) 引擎结算
        db2, pool2 = _make_pool_with_active(code="000002", entry_price=20.0, qty=100)
        conn2 = db2.get_connection()
        conn2.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn2.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                      ("000002", bj_date(), 18.0))
        conn2.commit()
        pool2.mark_exit_trigger("000002", 9.0, "止损")
        layer2 = self._make_layer(db2, pool2)
        layer2._settle_pending_exits()
        assert pool2.get("000002")["exit_source"] == "engine"

        # 2) 观察池 cleanup 过期退出
        conn2.execute("""
            INSERT INTO stock_pool (code, name, status, added_date, exit_source)
            VALUES ('000003', '过期股', 'watch', '2026-08-01', NULL)
        """)
        conn2.commit()
        expired = pool2.expire_stale_watch(expire_days=7)
        assert "000003" in expired
        assert pool2.get("000003")["exit_source"] == "cleanup"
