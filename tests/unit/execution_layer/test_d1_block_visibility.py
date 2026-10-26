"""D1 蓝卡续卡: check_entry 拦截原因可见化 — 红绿测试。

三卡（统筹派单 sm-b28a8697）：
① check_entry 所有拦截路径带结构化原因 {gate, detail}（宁 NULL 不可假值）；
② 拦截记录落库 entry_blocks 表（intraday_checks 留痕外的可查证台账）；
③ 报告面最小通路渲染拦截原因（午报候选跟踪段「被拦: 原因」）。

R3/D1 已合并（main=8f39500 起）：各闸门 stash `_block_reason` 文本 + checker
透出 blocked 结构 + monitor 推蓝卡 + intraday_checks status="blocked"。
本卡真增量 = 结构化 gate 字段 + 落库 + 渲染，纯增量不重写已有链路。
"""
import sqlite3

import pytest
from unittest.mock import MagicMock, patch

from execution_layer.core.entry_engine import EntryEngine
from execution_layer.tables import ENTRY_BLOCKS_DDL


def _pool_item(**over):
    item = {
        "code": "688349", "status": "pending_entry", "name": "三一重能",
        "entry_low": 28.0, "entry_high": 30.0, "valid_until": None,
        "stop_loss": 26.0, "added_reason": "风电景气",
    }
    item.update(over)
    return item


def _engine(pool_item):
    sp = MagicMock()
    sp.get.return_value = pool_item
    sp.action_record = MagicMock()
    return EntryEngine(db=MagicMock(), stock_pool=sp)


def _kline(close, open_=None):
    k = MagicMock()
    k.close = close
    k.open = open_ if open_ is not None else close
    return k


@pytest.fixture
def gate_pass():
    """默认放行 ProtectionGate / ConstraintChecker，只测目标闸门。"""
    with patch("execution_layer.core.protection_gate.ProtectionGate") as pg, \
         patch("execution_layer.core.constraint_checker.ConstraintChecker") as cc:
        pg.return_value.check.return_value = (True, "")
        cc.return_value.check_entry.return_value = MagicMock(
            passed=True, violations=[])
        yield pg, cc


# ---------- ① 结构化拦截原因 ----------

class TestBlockInfoStructured:
    """每个拦截路径 stash 结构化 `_block_info = {"gate": ..., "detail": ...}`。"""

    def test_expired_gate(self, gate_pass):
        eng = _engine(_pool_item(valid_until="2026-08-01"))
        assert eng.check_entry("688349", 29.0, "10:00") is None
        info = eng._block_info
        assert info["gate"] == "valid_until"
        assert info["detail"] is not None and "过期" in info["detail"]

    def test_protection_gate(self, gate_pass):
        pg, _ = gate_pass
        pg.return_value.check.return_value = (False, "回撤熔断中")
        eng = _engine(_pool_item())
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_info["gate"] == "protection"
        assert eng._block_info["detail"] == "回撤熔断中"

    def test_spike_defense_gate(self, gate_pass):
        dl = MagicMock()
        dl.get_history.return_value = MagicMock(
            klines=[_kline(10.0)] * 14 + [_kline(10.5, open_=10.0)])
        eng = _engine(_pool_item(entry_low=11.0, entry_high=13.0))
        eng.data_layer = dl
        assert eng.check_entry("688349", 12.5, "10:00") is None
        assert eng._block_info["gate"] == "spike_defense"
        assert eng._block_info["detail"] is not None

    def test_constraint_gate(self, gate_pass):
        _, cc = gate_pass
        cc.return_value.check_entry.return_value = MagicMock(
            passed=False, violations=["单行业超配"])
        eng = _engine(_pool_item())
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_info["gate"] == "constraint"
        assert "单行业超配" in eng._block_info["detail"]

    def test_agent_gate(self, gate_pass):
        eng = _engine(_pool_item())
        agent = MagicMock()
        resp = MagicMock()
        resp.verdict = "不推荐"
        resp.reasoning = "基本面恶化"
        agent.analyze.return_value = resp
        eng.agent_layer = agent
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_info["gate"] == "agent_review"
        assert "基本面恶化" in eng._block_info["detail"]

    def test_min_holding_gate(self, gate_pass):
        # 滚动日期: exit_date=今日 (而非硬编码施工当日) — 实现「距卖出
        # ≥1天放行」下, 昨日卖出的永远拦截, 硬编码日期 08-30 起永久假红
        # (Q7/canrun 同族病根)。
        from common.utils import bj_date
        eng = _engine(_pool_item(exit_date=bj_date()))
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_info["gate"] == "min_holding"
        assert eng._block_info["detail"] is not None

    def test_pass_and_out_of_range_leave_none(self, gate_pass):
        eng = _engine(_pool_item())
        decision = eng.check_entry("688349", 29.0, "10:00")
        assert decision is not None
        assert eng._block_info is None
        assert eng.check_entry("688349", 25.0, "10:00") is None
        assert eng._block_info is None  # 区间外非拦截, 无结构化原因

    def test_block_info_reset_between_calls(self, gate_pass):
        eng = _engine(_pool_item(valid_until="2026-08-01"))
        eng.check_entry("688349", 29.0, "10:00")
        assert eng._block_info is not None
        eng.stock_pool.get.return_value = _pool_item()
        eng.check_entry("688349", 29.0, "10:00")
        assert eng._block_info is None


# ---------- ② 拦截记录落库 ----------

class TestEntryBlocksTable:
    """ENTRY_BLOCKS_DDL 建表 + checker blocked 结构带 gate + monitor 落库。"""

    def _memdb(self):
        con = sqlite3.connect(":memory:")
        con.execute(ENTRY_BLOCKS_DDL)
        return con

    def test_ddl_creates_table(self):
        con = self._memdb()
        cols = [r[1] for r in con.execute("PRAGMA table_info(entry_blocks)")]
        assert set(cols) == {"id", "block_time", "code", "gate", "detail",
                              "price", "entry_low", "entry_high", "created_at"}
        # 索引 DDL 在 ALL_TABLES 清单内 (init_execution_tables 统一执行)
        from execution_layer.tables import ALL_TABLES
        assert any("idx_blocks_code" in s and "entry_blocks" in s
                   for s in ALL_TABLES)

    def test_checker_blocked_struct_carries_gate(self):
        from execution_layer.intraday.entry_checker import IntradayEntryChecker
        eng = MagicMock()
        eng.check_entry.return_value = None
        eng._block_reason = "回撤熔断中"
        eng._block_info = {"gate": "protection", "detail": "回撤熔断中"}
        chk = IntradayEntryChecker(MagicMock(), MagicMock(), eng,
                                   data_layer=None)
        chk._get_current_price = MagicMock(return_value=29.0)
        result = chk.check("688349", _pool_item())
        assert result["blocked"] is True
        assert result["gate"] == "protection"

    def test_monitor_records_block_row(self):
        from execution_layer.intraday.monitor import IntradayMonitor
        sp = MagicMock()
        sp.get_by_status.side_effect = lambda s: (
            [_pool_item()] if s == "pending_entry" else [])
        m = IntradayMonitor(db=MagicMock(), stock_pool=sp)
        m.entry_checker = MagicMock()
        m.entry_checker.check.return_value = {
            "price": 29.0, "reason": "回撤熔断中", "triggered": False,
            "blocked": True, "gate": "protection"}
        m.exit_checker = None
        m.paper_broker = MagicMock()
        m.touch_card = MagicMock()
        m._scan()
        # intraday_checks 留痕 (R3/D1 已有) 仍带 blocked
        rows = m.db.get_connection().executemany.call_args.args[1]
        assert [r[6] for r in rows] == ["blocked"]
        # 新: entry_blocks 落库一行, gate 字段就位
        # (conn.execute 调用多条, 从 call_args_list 找 entry_blocks 那笔)
        conn = m.db.get_connection()
        ins = next(c for c in conn.execute.call_args_list
                   if "INSERT INTO entry_blocks" in c.args[0])
        params = ins.args[1]
        assert params[1] == "688349"      # block_time 之后第一列 = code
        assert params[2] == "protection"  # gate
        assert "回撤熔断" in params[3]    # detail
        m.paper_broker.entry_with_fees.assert_not_called()


# 渲染测试的 stock_pool 列集 — 与 R3 午报跟踪测试 (test_r3_midday_tracking)
# 同口径: build_lifelines 需要全列 (created_at/exit_* 等), 缺列即空候选。
POOL_COLS = """
    code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
    entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
    entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
    dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
    exit_price REAL, exit_date TEXT, net_return_pct REAL, review_quality TEXT,
    source TEXT, initial_score REAL, entry_plan_source TEXT
"""

_LIFELINE_TABLES = """
    CREATE TABLE action_records (
        id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
        price REAL, reason TEXT, created_at TEXT);
    CREATE TABLE intraday_checks (
        id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
        current_price REAL, trigger_price REAL, status TEXT,
        detail TEXT, check_time TEXT);
    CREATE TABLE recommendation_retrospect (
        prediction_id INTEGER, code TEXT, rec_date TEXT, verdict TEXT,
        hit_5d INTEGER, hit_10d INTEGER,
        fwd_return_5d REAL, fwd_return_10d REAL);
    CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT);
"""


# ---------- ③ 报告面渲染 ----------

class TestMiddayRendersBlockReason:
    """午报候选跟踪段: 当日被拦候选渲染「被拦: gate 详情」。"""

    def _mkdb(self, with_block: bool) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.executescript(f"""
            CREATE TABLE stock_pool ({POOL_COLS});
            {_LIFELINE_TABLES}
        """)
        con.execute(ENTRY_BLOCKS_DDL)
        con.execute(
            "INSERT INTO stock_pool (code, name, status, added_date, created_at,"
            " entry_low, entry_high, valid_until, initial_score, source)"
            " VALUES ('688349','三一重能','pending_entry','2026-08-28','2026-08-28',"
            " 28.0, 30.0, '2026-09-05', 0.8, 'morning')")
        con.execute(
            "INSERT INTO stock_info VALUES ('688349','三一重能','风电设备')")
        if with_block:
            con.execute(
                "INSERT INTO entry_blocks (block_time, code, gate, detail,"
                " price, entry_low, entry_high) VALUES"
                " ('2026-08-29 10:05:00','688349','protection','回撤熔断中',"
                " 29.0, 28.0, 30.0)")
        con.commit()
        return con

    def _ctx(self, con):
        from report_layer.reports.base import ReportContext
        data = MagicMock()
        data.db.get_connection.return_value = con
        data.get_realtime_prices.return_value = {"688349": 29.0}
        return ReportContext(date="2026-08-29", report_mode="debug",
                             data_layer=data)

    def test_renders_blocked_line(self):
        from report_layer.sections.midday_candidate_tracking import (
            MiddayCandidateTrackingSection,
        )
        con = self._mkdb(with_block=True)
        result = MiddayCandidateTrackingSection().build(self._ctx(con))
        assert "688349" in result.body
        assert "被拦" in result.body
        assert "回撤熔断" in result.body

    def test_honest_empty_when_no_blocks(self):
        """无拦截记录时不得虚构被拦行 (诚实水位, 范式 7)。"""
        from report_layer.sections.midday_candidate_tracking import (
            MiddayCandidateTrackingSection,
        )
        con = self._mkdb(with_block=False)
        result = MiddayCandidateTrackingSection().build(self._ctx(con))
        assert "688349" in result.body      # 候选行照常
        assert "被拦" not in (result.body or "")
