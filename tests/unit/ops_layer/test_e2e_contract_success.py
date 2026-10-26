# -*- coding: utf-8 -*-
"""B1: e2e DDP「有效成功」判据 (2026-10-21)。

A2a 契约 (qs-handoff/DDP_CONTRACT_20260920.md): DDP 父状态字符串不能独断;
critical={kline,valuation} 全 success 且 kline>=4000 且降级全具名 tolerated
才是有效成功。边界: 只动 daily_data_pipeline; 其余管道裸判定不变;
failed 永不豁免; WFO/策略/回测等非 DDP 管道一律不放水。
"""
import importlib.util
import os
import sqlite3
from types import SimpleNamespace

DATE = "2026-10-20"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _load():
    spec = importlib.util.spec_from_file_location(
        "e2e_verification",
        os.path.join(ROOT, "scripts", "e2e_verification.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def _app(rows, kline_rows=5000):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = lambda c, r: {k[0]: r[i]
                                     for i, k in enumerate(c.description)}
    conn.execute("CREATE TABLE pipeline_runs (pipeline_name TEXT, status TEXT,"
                 " error_msg TEXT, duration_s REAL, date TEXT)")
    conn.executemany("INSERT INTO pipeline_runs VALUES (?,?,?,?,?)", rows)
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT)")
    conn.executemany("INSERT INTO daily_klines VALUES (?,?)",
                     [("000001", DATE)] * kline_rows)
    conn.commit()
    return SimpleNamespace(
        db=SimpleNamespace(get_connection=lambda: conn))


def _status(app, pipes):
    checks = MOD.check_pipelines(app, DATE, pipes)
    return {c["name"]: (c["status"], c["detail"]) for c in checks}


def _ddp_row(status, err):
    return ("daily_data_pipeline", status, err, 12.5, DATE)


def test_ddp_partial_tolerated_only_is_effective_success():
    """正向钉: DDP partial 但降级全具名 tolerated + kline 足额 → pass。"""
    app = _app([_ddp_row("partial", "lhb_daily: partial (当日未发布); "
                                   "index_daily: partial (缺少业务日)")])
    st, detail = _status(app, ["daily_data_pipeline"])["daily_data_pipeline"]
    assert st == "pass", "tolerated 具名降级应判有效成功: %s" % detail


def test_ddp_partial_with_critical_leg_is_fail():
    """反向钉: 降级含 critical 腿 (kline) → 必 fail。"""
    app = _app([_ddp_row("partial", "kline: partial (覆盖率不足); "
                                   "lhb_daily: partial (当日未发布)")])
    st, detail = _status(app, ["daily_data_pipeline"])["daily_data_pipeline"]
    assert st == "fail", "critical 腿降级必须 fail: %s" % detail


def test_ddp_partial_with_unnamed_leg_is_fail():
    """第三钉: 降级含非白名单腿 → 必 fail (白名单封闭)。"""
    app = _app([_ddp_row("partial", "mystery_leg: partial (未知降级)")])
    st, detail = _status(app, ["daily_data_pipeline"])["daily_data_pipeline"]
    assert st == "fail", "未具名/非白名单腿降级必须 fail: %s" % detail


def test_ddp_failed_never_exempt():
    """failed 永不豁免, 即使 error 全是 tolerated 腿。"""
    app = _app([_ddp_row("failed", "lhb_daily: failed (源异常); "
                                  "index_daily: failed (源异常)")])
    st, detail = _status(app, ["daily_data_pipeline"])["daily_data_pipeline"]
    assert st == "fail", "failed 必须 fail: %s" % detail


def test_other_pipe_partial_stays_fail():
    """边界钉: 非 DDP 管道 partial 仍裸判 fail (不放水 WFO/策略/回测)。"""
    app = _app([("factor_update", "partial", "factor_x: partial (缺数)",
                 5.0, DATE)])
    st, detail = _status(app, ["factor_update"])["factor_update"]
    assert st == "fail", "非 DDP 管道不得放水: %s" % detail
