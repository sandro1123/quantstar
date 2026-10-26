# -*- coding: utf-8 -*-
"""no_stocks 早退观测钉 — TDD RED 测试面 (feat/no-stocks-obs, 10-19 派单①)。

设计意图 (W1 10-07 定谳第二缺口, 统筹 10-19 派单①落地):
  no_stocks 早退今天分不清两种病 —
    假日早退 (正常): 当日非交易日, kline 0 行是天的属性
    源挂早退 (病):   当日是交易日, kline 0 行是源/管道的锅
  两者 strategy_runs 全落 no_stocks / stock_count=0 同形, results_hash=''
  事后回看无法区分 (H-1 追因踩过的坑: 表象同貌 ≠ 同因)。

⚠️ 方案修订记录 (TDD RED 第一轮自否):
  初版设想「results dict → results_hash 里解码」被 self-check 否掉 —
  finish_run 的 results_hash = md5(results)[:8] 是**单向散列**, 落库后
  原 dict 不可恢复 (hash 只为对拍一致性存在)。观测键要「事后回看」,
  必须原样落库 → 新列 results_json (provenance DDL 新增 + promote
  ALTER TABLE ADD COLUMN 兼容存量库)。

新行为契约:
  1. no_stocks 行 results_json 非空, 可 json.loads 回 kline_rows+trading_day 两键
  2. kline_rows = 当日 daily_klines 真实 COUNT(*) (data_layer.count_daily_klines)
  3. trading_day = TradingCalendar 判定 (db 口径, fail-open 静态口径兜底)
  4. 两键组合可判: trading_day=True & kline_rows=0 → 源挂 (假日早退被排除)
                   trading_day=False → 假日早退 (kline_rows 必 0 同证)
  5. 计数面失败 (db 挂) → kline_rows=None + fail_count=True, 不静默 0
     (判例: 读不到 ≠ 真的是 0; 空集与零命中必须可区分)
  6. 其他 status 路径 (success/select_failclosed) 的 results 面零改动 (对拍);
     success 行 results_json 也落 ({"signals":[...]} 原样), hash 算法不变
"""
import json
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from common.database import Database
from strategy_layer import StrategyLayer
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.provenance import ProvenanceTracker
from strategy_layer.signals.generator import TradeSignal

DATE = "2026-10-09"  # 周五交易日 (国庆节后首日, TradingCalendar 静态口径 True) — kline_rows=0 即源挂形态
HOLIDAY = "2026-10-02"  # 周五国庆假日 (日历表内实测 False) — 假日早退形态
CODE = "000001.SZ"


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path), path


def _make_layer(db, enabled=True):
    layer = StrategyLayer()
    layer.db = db
    layer.config = {"strategy.provenance.enabled": enabled}
    layer.data_layer = MagicMock()
    layer.data_layer.get_risky_codes.return_value = set()
    return layer


def _mock_chain(st, layer, codes=None):
    codes = [CODE] if codes is None else codes
    fd = FactorDaily(code=CODE, date=DATE, composite_score=1.0,
                     factor_scores={"momentum_20d": 2.0})
    sig = TradeSignal(code=CODE, date=DATE, action="buy", signal_id="sigX")
    st.enter_context(patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()))
    st.enter_context(patch.object(layer, "_get_stock_codes", return_value=codes))
    st.enter_context(patch.object(layer.aggregator, "aggregate", return_value=[fd]))
    st.enter_context(patch.object(layer.aggregator, "fill_recent_returns"))
    st.enter_context(patch.object(layer, "market_regime", return_value="neutral"))
    reg_cls = st.enter_context(patch("strategy_layer.ml.registry.ModelRegistry"))
    reg_cls.return_value.list_versions.return_value = []
    st.enter_context(patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])))
    st.enter_context(patch.object(layer.deep_review, "review_candidates", return_value=[]))
    st.enter_context(patch.object(layer.signal_generator, "generate", return_value=[sig]))


def _results_json_of(tracker, run_id_date=DATE):
    """从 strategy_runs 读回 results_json → 解码 dict (观测钉核心: 事后可回看)。"""
    rows = [r for r in tracker.list_runs(10) if r["date"] == run_id_date]
    assert len(rows) == 1, f"应有 1 行 {run_id_date}, 实际 {len(rows)}"
    rj = rows[0].get("results_json")
    assert rj, "results_json 非空 (钉1: 观测键必须原样落库, 不是 hash)"
    return json.loads(rj)


class TestNoStocksObservation:
    def test_trading_day_zero_kline_is_source_outage(self):
        """交易日 + kline 0 行 → kline_rows=0 & trading_day=True (源挂形态可判)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[])
                with patch.object(layer.data_layer, "count_daily_klines", return_value=0) as cnt:
                    layer.run_daily(DATE)
            assert cnt.called, "no_stocks 分支必须真去数当日 kline 行数 (钉2)"
            results = _results_json_of(ProvenanceTracker(db=db))
            assert results["trading_day"] is True, "2024-01-02 是交易日"
            assert results["kline_rows"] == 0, "kline_rows 落库真实计数"
            assert results.get("fail_count") is not True, "计数成功不标 fail"
        finally:
            os.unlink(path)

    def test_holiday_zero_kline_is_normal(self):
        """假日 + kline 0 行 → trading_day=False (假日早退形态可判, 与源挂区分)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[])
                with patch.object(layer.data_layer, "count_daily_klines", return_value=0):
                    layer.run_daily(HOLIDAY)
            results = _results_json_of(ProvenanceTracker(db=db), HOLIDAY)
            assert results["trading_day"] is False, "2024-01-01 元旦假日"
            assert results["kline_rows"] == 0
        finally:
            os.unlink(path)

    def test_nonzero_kline_landed(self):
        """kline 有行但 codes 仍空 (边界) → kline_rows 如实落非 0, 不伪装成源挂。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[])
                with patch.object(layer.data_layer, "count_daily_klines", return_value=5573):
                    layer.run_daily(DATE)
            results = _results_json_of(ProvenanceTracker(db=db))
            assert results["kline_rows"] == 5573, "真实行数如实落, 不抹零"
        finally:
            os.unlink(path)

    def test_count_failure_leaves_trace_not_silent_none(self):
        """计数面炸 (db 挂) → kline_rows=None + fail_count=True, 不静默 0。
        判例: 读不到 ≠ 真的是 0 (空集与零命中必须可区分)。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[])
                with patch.object(layer.data_layer, "count_daily_klines",
                                  side_effect=RuntimeError("db down")):
                    layer.run_daily(DATE)
            results = _results_json_of(ProvenanceTracker(db=db))
            assert results.get("fail_count") is True, "读不到显式标 fail, 不与真 0 混淆"
            assert results.get("kline_rows") is None, "读不到落 None 而非 0"
        finally:
            os.unlink(path)

    def test_success_path_results_json_lands_verbatim(self):
        """对拍: success 路径 results_json 原样落 (signals 列表), hash 算法不变。"""
        db, path = _tmp_db()
        try:
            layer = _make_layer(db)
            with ExitStack() as st:
                _mock_chain(st, layer, codes=[CODE])
                layer.run_daily(DATE)
            tracker = ProvenanceTracker(db=db)
            rows = [x for x in tracker.list_runs(10) if x["date"] == DATE]
            r = rows[0]
            assert r["status"] == "success"
            results = json.loads(r["results_json"])
            assert results == {"signals": [CODE]}, "与原 results 结构逐位一致"
            expected_hash = __import__("hashlib").md5(
                json.dumps(results, sort_keys=True).encode()).hexdigest()[:8]
            assert r["results_hash"] == expected_hash, "hash 算法不变 (存量对拍口径)"
        finally:
            os.unlink(path)

    def test_ddl_adds_results_json_column(self):
        """DDL 契约: _ensure_table 后 strategy_runs 有 results_json 列 (新库)。"""
        db, path = _tmp_db()
        try:
            ProvenanceTracker(db=db)
            cols = [c[1] for c in db.get_connection().execute(
                "PRAGMA table_info(strategy_runs)").fetchall()]
            assert "results_json" in cols, f"strategy_runs 应有 results_json 列, 实际 {cols}"
        finally:
            os.unlink(path)

    def test_legacy_db_gets_column_via_ensure(self):
        """存量库兼容: 无 results_json 列的旧 strategy_runs → _ensure_table 补齐
        (promote ALTER TABLE ADD COLUMN 同构; 不因缺列炸 finish_run)。"""
        db, path = _tmp_db()
        try:
            conn = db.get_connection()
            conn.execute("""CREATE TABLE strategy_runs (
                run_id TEXT PRIMARY KEY, date TEXT NOT NULL, mode TEXT DEFAULT 'live',
                code_commit TEXT, config_hash TEXT, parameters TEXT, results_hash TEXT,
                stock_count INTEGER DEFAULT 0, signal_count INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0, status TEXT DEFAULT 'running',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            conn.commit()
            ProvenanceTracker(db=db)  # _ensure_table 应补列
            cols = [c[1] for c in conn.execute(
                "PRAGMA table_info(strategy_runs)").fetchall()]
            assert "results_json" in cols, "旧库 ensure 后补齐 results_json"
        finally:
            os.unlink(path)
