# -*- coding: utf-8 -*-
"""D3 K线补全第二段测试: 停牌复牌检测+补全常态化 (RED→GREEN)
统筹派单 sm-46287f4038b242a3 — qs-d3b @ feat/d3-kline-complete

probe 勘正 (A 席取证, 派单技术细节=工作假设判例再实证):
  ①「13 只」实为主板 6 只真停牌族 (002731/002870/301139/600929/688432/688801-N燧原全无)
    — 其余 ~250 只为北交所/新三板段 (BJ 43/83/87/92 段止于 07-17=已知结构性缺口非停牌面);
  ②D4 source_crosscheck 至今零表零 runs (「桶A 已会每日对拍出漏采清单」非实据)
    — 漏采检测须本管道自建。

设计: suspend_recovery 管道 (注册进 PipelineRegistry, 不进 DDP 子腿清单 —
  与 concept_plates_daily 同型: 只注册供单独调起, 不挂 orchestrator 自动序)。
  ① 复牌检测: 主板 active 股 last_kline < 基准日-N 交易日 → 候选;
     baostock get_daily_trade_status(date) 批量判 tradestatus;
     停牌中 (status=0) → 正确缺席零动作; 复牌日 (status=1 且库内无行) → get_daily_market 补拉。
  ② 常态化补全: 当日市场行集差 = 备源全市场快照有而库内无的 (code,date) → 定向补拉。
  ③ 幂等: batch_upsert INSERT OR REPLACE + 补前查重 (已存在行不重拉)。
"""
import sys
from unittest.mock import MagicMock

import pytest

import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[3] / "src"))

# AG-WIRE-1 判例: 剔除可能驻留的他树模块, 强制从本树解析。
# 09-14 跨域污染清偿 (wfo_concept_coverage 合跑 FF 卡): 无条件清空会把先收集
# 模块 (test_wfo_concept_coverage) 已绑定的 data_layer.DataLayer 类对象换成
# 新模块对象 — isinstance 真源判别失效, concept 预检静默跳过 (合跑红/单跑绿)。
# 改条件剔除: 仅清 __file__ 落在本树 src 之外的驻留模块, 同树模块保留,
# 类身份不分裂; 他树驻留场景由 guard_cross_domain_pollution 兜红。
_SRC = str(_pl.Path(__file__).resolve().parents[3] / "src")
for _m in [m for m in list(sys.modules) if m == "data_layer" or m.startswith("data_layer.")]:
    if not (getattr(sys.modules[_m], "__file__", None) or "").startswith(_SRC):
        del sys.modules[_m]

from data_layer.pipelines.base import PipelineInput, PipelineResult  # noqa: E402
from data_layer.pipelines import suspend_recovery as sr  # noqa: E402
import data_layer  # noqa: E402

assert data_layer.__file__.startswith(
    str(_pl.Path(__file__).resolve().parents[3] / "src")), data_layer.__file__  # 防御: 本树解析锚 (相对推导, 无硬编码)


# ---------------------------------------------------------------------------
# 夹具: 只读内存库 (schema 仿 repos/kline._ensure_table + stock_info 最小列)
# ---------------------------------------------------------------------------

@pytest.fixture()
def memdb():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE daily_klines (
        code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        pre_close REAL, change_pct REAL, turnover_rate REAL, adjust_type TEXT DEFAULT 'raw',
        PRIMARY KEY (code, date))""")
    conn.execute("""CREATE TABLE stock_info (
        code TEXT PRIMARY KEY, name TEXT, market TEXT, is_active INTEGER DEFAULT 1)""")
    db = MagicMock()
    db.get_connection.return_value = conn
    yield db, conn
    conn.close()


def _seed_calendar(conn, days=("2026-09-10", "2026-09-09", "2026-09-08", "2026-09-07")):
    """全市场日历行: 000001 每个交易日有行 (真实库形态 — 停牌股缺席不影响全市场日历)。"""
    for d in days:
        conn.execute("INSERT OR IGNORE INTO daily_klines (code,date,close) VALUES ('000001',?,10.0)", (d,))
    conn.commit()


def _mk_record(code, date, close=10.0):
    return {"code": code, "date": date, "open": 9.9, "high": 10.1, "low": 9.8,
            "close": close, "volume": 1000, "amount": 10000,
            "pre_close": 9.9, "change_pct": 1.0, "turnover_rate": 0.5}


def _mk_sources(trade_status=None, market_rows=None):
    """备源替身: trade_status={code:bool} | None; market_rows=按日返回的 record 列表"""
    src = MagicMock()
    def _status(date):
        return trade_status
    def _market(date):
        return [r for r in (market_rows or []) if r["date"] == date]
    src.name.return_value = "baostock"
    src.get_daily_trade_status.side_effect = _status
    src.get_daily_market.side_effect = _market
    return src


# ---------------------------------------------------------------------------
# ① 复牌检测
# ---------------------------------------------------------------------------

class TestResumeDetection:
    def test_suspended_stock_no_kline_no_action(self, memdb):
        """停牌中 (tradestatus=0) → 正确缺席: 不补拉, 报 suspended 计数。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('600929','雪天盐业','SH',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('600929','2026-08-28',10.0)")
        _seed_calendar(conn)
        src = _mk_sources(trade_status={"600929": False})
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.status == "success"
        assert res.metadata.get("suspended_n") == 1
        assert res.metadata.get("recovered_n") == 0
        # 停牌股 09-10 无新增行 (日历行 000001 不在此断言域)
        n = conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE code='600929' AND date='2026-09-10'").fetchone()[0]
        assert n == 0

    def test_resumed_stock_backfilled(self, memdb):
        """复牌日 (tradestatus=1 且库内无该日行) → get_daily_market 补拉+落库。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('600929','雪天盐业','SH',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('600929','2026-08-28',10.0)")
        _seed_calendar(conn)
        market = [_mk_record("600929", "2026-09-10", 11.0), _mk_record("000001", "2026-09-10")]
        src = _mk_sources(trade_status={"600929": True}, market_rows=market)
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("recovered_n") == 1
        row = conn.execute(
            "SELECT close FROM daily_klines WHERE code='600929' AND date='2026-09-10'").fetchone()
        assert row is not None and abs(row[0] - 11.0) < 1e-9

    def test_recent_kline_stock_not_candidate(self, memdb):
        """K 线新鲜 (≥基准窗) 的股不是复牌候选 → 零探询零动作。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('000001','平安','SZ',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('000001','2026-09-10',10.0)")
        src = _mk_sources()
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("suspended_n") == 0
        src.get_daily_trade_status.assert_not_called()

    def test_no_kline_at_all_marks_new_listing_not_suspended(self, memdb):
        """全无 K 线 + tradestatus=1 → 判新股/次新 (非停牌族), 走补拉但标 no_history。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('688801','N燧原','SH',1)")
        _seed_calendar(conn)
        market = [_mk_record("688801", "2026-09-10", 50.0), _mk_record("000001", "2026-09-10")]
        src = _mk_sources(trade_status={"688801": True}, market_rows=market)
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("no_history_n") == 1
        n = conn.execute("SELECT COUNT(*) FROM daily_klines WHERE code='688801'").fetchone()[0]
        assert n == 1

    def test_bj_segment_excluded(self, memdb):
        """北交所/新三板段 (43/83/87/92/82 开头) 不入复牌候选 (已知结构性缺口另卡)。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('430017','星昊医药','BJ',1)")
        src = _mk_sources(trade_status={"430017": True})
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("suspended_n") == 0
        assert res.metadata.get("candidates_n") == 0

    def test_trade_status_none_degrades_honestly(self, memdb):
        """trade_status 查询失败 (None) → 诚实降级: status=partial + errors 留痕, 禁静默。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('600929','雪天盐业','SH',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('600929','2026-08-28',10.0)")
        _seed_calendar(conn)
        src = _mk_sources(trade_status=None)
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.status == "partial"
        assert any("trade_status" in str(e) for e in res.errors)


# ---------------------------------------------------------------------------
# ② 补全常态化 (当日漏采定向补)
# ---------------------------------------------------------------------------

class TestDailyGapFill:
    def test_gap_rows_backfilled(self, memdb):
        """当日库内缺行 (备源有) → 定向补拉落库+落账。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('000001','平安','SZ',1)")
        conn.execute("INSERT INTO stock_info VALUES ('600519','茅台','SH',1)")
        # 000001 当日有, 600519 当日缺
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('000001','2026-09-10',10.0)")
        market = [_mk_record("600519", "2026-09-10", 1500.0)]
        src = _mk_sources(market_rows=[_mk_record("000001", "2026-09-10")] + market)
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("gap_filled_n") == 1
        row = conn.execute(
            "SELECT close FROM daily_klines WHERE code='600519' AND date='2026-09-10'").fetchone()
        assert row is not None and abs(row[0] - 1500.0) < 1e-9

    def test_existing_rows_not_refetched(self, memdb):
        """库内已有行不重复补 (补前查重); 备源重复返回同 code 不产新行。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('000001','平安','SZ',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('000001','2026-09-10',10.0)")
        src = _mk_sources(market_rows=[_mk_record("000001", "2026-09-10", 99.0)])
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.metadata.get("gap_filled_n") == 0
        # 已存在行不被备源数据覆盖 (补前查重语义)
        row = conn.execute(
            "SELECT close FROM daily_klines WHERE code='000001' AND date='2026-09-10'").fetchone()
        assert abs(row[0] - 10.0) < 1e-9

    def test_empty_market_degrades(self, memdb):
        """备源当日空返回 (非交易日/超时后空) → 零动作+落账 attempted=0。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('000001','平安','SZ',1)")
        src = _mk_sources(market_rows=[])
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res.status in ("success", "partial")
        assert res.metadata.get("gap_filled_n") == 0


# ---------------------------------------------------------------------------
# ③ 幂等
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_double_run_zero_duplicate_writes(self, memdb):
        """双跑零重复写: PK 计数不变, recovered 二跑为 0 (已补齐不再缺)。"""
        db, conn = memdb
        conn.execute("INSERT INTO stock_info VALUES ('600929','雪天盐业','SH',1)")
        conn.execute("INSERT INTO daily_klines (code,date,close) VALUES ('600929','2026-08-28',10.0)")
        _seed_calendar(conn)
        market = [_mk_record("600929", "2026-09-10", 11.0), _mk_record("000001", "2026-09-10")]
        src = _mk_sources(trade_status={"600929": True}, market_rows=market)
        pipe = sr.SuspendRecoveryPipeline(source=src, resume_lookback_days=3)
        res1 = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        assert res1.metadata.get("recovered_n") == 1
        n1 = conn.execute("SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        res2 = pipe.run(PipelineInput(db=db, date="2026-09-10"))
        n2 = conn.execute("SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        assert n1 == n2
        assert res2.metadata.get("recovered_n") == 0
        assert res2.metadata.get("gap_filled_n") == 0
