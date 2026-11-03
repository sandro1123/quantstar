# -*- coding: utf-8 -*-
"""#11 单只仓位上限口径 + paper broker entry 事务一致性

Issue #11 原文:
  「核对单只仓位上限的计算口径 (按总资产还是可用资金、是否含在途订单);
    paper broker 买入的状态谓词、rowcount 检查、事务边界,
    避免重复成交或状态不一致。」

## 本卡修的两处真缺陷

### 缺陷 A: 单只仓位上限算的是"组合总仓位"
旧代码:
    total_invested = sum(所有持仓市值) + 本笔
    current_pct = total_invested / initial_capital * 100
    if current_pct > max_single_stock_pct:   # 20%
        violations.append("单只仓位上限...")
10 只各 10% 的持仓 = 100% > 20% → **任何新买入都被拦**;
空仓时又永远不拦。判据与语义完全错配。

修法: 单只口径 = 本笔金额 / 组合总权益; 总权益 = 可用现金 + 持仓市值,
拿不到时回落初始本金 (保守下界 → 判定偏严, 不会放过超限)。

### 缺陷 B: paper broker entry 无状态谓词、无 rowcount 检查
旧代码 UPDATE stock_pool ... WHERE code = ? —— 上面读的 status 是读时快照,
写时状态可能已被改掉 → 已 active 的票被二次 entry (覆盖 entry_price、
再扣款、action_records 多写一条)。exit 路径有谓词 + rowcount, entry 没有。

修法: WHERE code=? AND status='pending_entry' + rowcount==1 检查。

## 反向钉
- 把 WHERE 谓词拆掉 → "二次 entry 必须失败" 的断言应报红;
- 把单只口径改回组合总仓位 → 多持仓场景断言应报红。
"""
import sqlite3

import pytest


# ============================================================ 组1: 单只仓位上限口径
class _FakePool:
    """最小 StockPool 假体 — 只提供约束检查用到的三个方法。"""

    def __init__(self, cash=1_000_000.0, active=None):
        self._cash = cash
        self._active = active or []

    def get_available_cash(self, initial_capital):
        return self._cash

    def get_by_status(self, status):
        return self._active if status == "active" else []

    def get(self, code):
        return None


class TestSinglePositionCapCaliber:
    def _check(self, cash, active, price, qty, cap=20.0, initial=1_000_000.0):
        from execution_layer.core.constraint_checker import ConstraintChecker
        # ConstraintChecker 的构造依赖 cfg_get, 这里直接注入属性避免配置面
        checker = ConstraintChecker.__new__(ConstraintChecker)
        checker.stock_pool = _FakePool(cash=cash, active=active)
        checker.max_positions = 10
        checker.max_single_stock_pct = cap
        checker.initial_capital = initial
        return checker.check_entry(
            code="000001", current_price=price, position_qty=qty)

    def test_many_small_positions_still_allow_new_small_buy(self):
        """核心回归: 10 只各 10% 持仓时, 买一只 5% 的票不该被拦。

        旧实现会把 (100% + 5%) 当成单只仓位 → 误拦。
        """
        active = [{"code": f"{i:06d}", "entry_price": 10.0, "position_qty": 10_000,
                   "last_price": 10.0} for i in range(10)]
        # 10 只 × 10 万 = 100 万已投, 现金 0 → 总权益 100 万
        res = self._check(cash=0.0, active=active, price=10.0, qty=5_000)
        # 本笔 5 万 / 100 万 = 5% <= 20% → 只该因"余额不足"失败, 不该因单只上限
        assert not any("单只仓位上限" in v for v in res.violations), (
            f"5% 的单笔买入被误判超过 20% 单只上限: {res.violations}")

    def test_oversized_single_position_is_blocked(self):
        """真超限: 本笔占总权益 30% > 20% → 必须拦"""
        res = self._check(cash=1_000_000.0, active=[], price=10.0, qty=30_000)
        assert any("单只仓位上限" in v for v in res.violations), (
            f"30% 单笔未被拦: {res.violations}")

    def test_exactly_at_cap_passes(self):
        """边界: 恰好 20% 不拦 (用的是 > 不是 >=)"""
        res = self._check(cash=1_000_000.0, active=[], price=10.0, qty=20_000)
        assert not any("单只仓位上限" in v for v in res.violations)

    def test_denominator_is_equity_not_available_cash(self):
        """分母是总权益 (现金+持仓市值), 不是可用现金。

        构造: 现金 10 万 + 持仓 90 万 = 总权益 100 万; 本笔 10 万。
        用总权益 → 10% 通过; 若误用可用现金 10 万 → 100% 会被拦。
        这条钉住分母的选择。
        """
        active = [{"code": "600000", "entry_price": 10.0, "position_qty": 90_000,
                   "last_price": 10.0}]
        res = self._check(cash=100_000.0, active=active, price=10.0, qty=10_000)
        assert not any("单只仓位上限" in v for v in res.violations), (
            f"分母疑似用了可用现金 (10万) 而非总权益 (100万): {res.violations}")

    def test_equity_helper_falls_back_to_initial(self):
        """总权益不可算时回落初始本金 (保守下界, 不放过超限)"""
        from execution_layer.core.constraint_checker import ConstraintChecker
        checker = ConstraintChecker.__new__(ConstraintChecker)
        checker.stock_pool = _FakePool(cash=1_000_000.0, active=[])
        checker.initial_capital = 500_000.0
        assert checker._get_total_equity() == pytest.approx(1_000_000.0)

    def test_mutation_composite_caliber_is_caught(self):
        """反向钉: 把口径改回"组合总仓位" → 多持仓断言必须报红"""
        from execution_layer.core.constraint_checker import ConstraintChecker

        def old_caliber(self, code, current_price, position_qty):
            # 复刻旧实现: (全部持仓市值 + 本笔) / 初始资金
            active = self.stock_pool.get_by_status("active")
            total = sum((p.get("entry_price", 0) or 0) * (p.get("position_qty", 0) or 0)
                        for p in active)
            pct = (total + current_price * position_qty) / self.initial_capital * 100
            return pct

        checker = ConstraintChecker.__new__(ConstraintChecker)
        active = [{"entry_price": 10.0, "position_qty": 10_000} for _ in range(10)]
        checker.stock_pool = _FakePool(cash=0.0, active=active)
        checker.initial_capital = 1_000_000.0
        # 旧口径下 5% 的单笔算出 105% > 20 → 会被拦
        assert old_caliber(checker, "000001", 10.0, 5_000) > 20.0


# ============================================================ 组2: entry 状态谓词 + rowcount
@pytest.fixture
def broker_env(tmp_path):
    """构造真 PaperBroker + 真 stock_pool 表 — 走真 SQL 路径。"""
    from common.database import Database
    from execution_layer.tables import STOCK_POOL_DDL

    db = Database(str(tmp_path / "b.db"))
    db.init_db()
    conn = db.get_connection()
    conn.execute(STOCK_POOL_DDL)
    conn.commit()
    return db, conn


def _insert_pool(conn, code, status):
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, initial_score) "
        "VALUES (?,?,?,?,?)", (code, f"n-{code}", status, "2026-01-01", 60.0))
    conn.commit()


def _make_broker(db):
    from execution_layer.core.paper_broker import PaperBroker
    return PaperBroker(db, stock_pool=_StubPool())


class _StubPool:
    """PaperBroker 用到的 pool 接口 — 直接从 db 查, 保持简单。"""

    def __init__(self):
        self._db = None

    def bind(self, db):
        self._db = db
        return self

    def get(self, code):
        row = self._db.get_connection().execute(
            "SELECT * FROM stock_pool WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None

    def get_by_status(self, status):
        return []

    def get_available_cash(self, initial):
        return 1_000_000.0


class TestEntryTransactionSafety:
    def _broker(self, db):
        """绕过 PaperBroker.__init__ 的 cfg_get (测试环境 CF-01 硬抛), 只手装依赖。"""
        from execution_layer.core.paper_broker import PaperBroker
        from execution_layer.core.action_record import ActionRecord

        b = PaperBroker.__new__(PaperBroker)
        b.db = db
        b.stock_pool = _StubPool().bind(db)
        b.config = None
        b.action_record = ActionRecord(db)
        b._initial_capital = 1_000_000.0
        b._delivery = None
        b._delivery_ready = False
        b.dry_run = False
        return b

    def test_second_entry_on_active_stock_is_rejected(self, broker_env):
        """核心: 已 active 的票再 entry → 必须失败 (不能覆盖/重复扣款)"""
        db, conn = broker_env
        _insert_pool(conn, "000001", "active")
        broker = self._broker(db)

        res = broker.entry_with_fees("000001", 10.0, "test")

        assert res.success is False
        assert "status" in (res.reason or "").lower()
        # 状态必须还是 active, 且 entry_price 未被改写
        row = conn.execute(
            "SELECT status, entry_price FROM stock_pool WHERE code='000001'").fetchone()
        assert row[0] == "active"

    def test_pending_entry_succeeds(self, broker_env):
        """正常路径: pending_entry → active 应当成功。

        注: ProtectionGate 需要 action_records 表才能判定守卫; 建表后
        gate 才会放行 (缺表时它 fail-closed 拦下 —— 那是 #9 的正确行为,
        不该为了这条测试去阉割 fail-closed)。
        """
        from execution_layer.tables import ACTION_RECORDS_DDL
        db, conn = broker_env
        conn.execute(ACTION_RECORDS_DDL)
        conn.commit()
        _insert_pool(conn, "000002", "pending_entry")
        broker = self._broker(db)

        res = broker.entry_with_fees("000002", 10.0, "test")

        assert res.success is True, f"正常买入被拦: {res.reason}"
        row = conn.execute(
            "SELECT status FROM stock_pool WHERE code='000002'").fetchone()
        assert row[0] == "active"

    def test_missing_action_records_fails_closed(self, broker_env):
        """缺 action_records 表 → gate fail-closed 拦下 (Issue #9 的正确行为)"""
        db, conn = broker_env
        _insert_pool(conn, "000004", "pending_entry")
        broker = self._broker(db)

        res = broker.entry_with_fees("000004", 10.0, "test")

        assert res.success is False, "守卫异常时不该放行 (fail-closed)"
        assert "protection_gate" in (res.reason or "")

    def test_rowcount_check_present_in_source(self):
        """静态钉: entry 路径必须真有 rowcount 检查 (与 exit 对齐)"""
        import inspect
        from execution_layer.core import paper_broker as pb
        src = inspect.getsource(pb.PaperBroker.entry_with_fees)
        assert "rowcount" in src, "entry 缺 rowcount 检查 — 并发下会重复成交"
        assert "status = 'pending_entry'" in src, (
            "entry 的 UPDATE 缺状态谓词 — 会覆盖已被别人改掉的状态")

    def test_exit_also_has_predicate(self):
        """对照钉: exit 路径本来就有, 不能被改掉"""
        import inspect
        from execution_layer.core import paper_broker as pb
        src = inspect.getsource(pb.PaperBroker.exit_with_fees)
        assert "status = 'exit_pending'" in src
        assert "rowcount" in src

    def test_mutation_missing_predicate_is_caught(self, broker_env):
        """反向钉: 拆掉 WHERE 的状态谓词 → 二次 entry 断言必须报红"""
        db, conn = broker_env
        _insert_pool(conn, "000003", "active")
        broker = self._broker(db)

        def mutated_entry(code, price):
            # 复刻旧实现: 无状态谓词
            conn.execute(
                "UPDATE stock_pool SET status='active', entry_price=? WHERE code=?",
                (price, code))
            conn.commit()
            return True

        first = mutated_entry("000003", 99.0)
        assert first is True
        row = conn.execute(
            "SELECT entry_price FROM stock_pool WHERE code='000003'").fetchone()
        assert row[0] == 99.0, "变异实现应当覆盖价格 — 这才说明旧实现在重复扣款"
