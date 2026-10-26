# -*- coding: utf-8 -*-
"""task#34 (2026-09-04): gap_summary 增报 kline-backed 可填数 — TDD RED 先行底稿。

设计稿 (vault 分层讨论/gap_summary可填数增报设计稿/B, md5 f437fdce) 案A
+ 统筹 GO 两补 (sm-0d660d8d):
1. gap_summary 事件扩三口径: kline_backed_by_horizon (可填上界) +
   structural_by_horizon (= null − kline_backed, 结构性不可填直接可读)。
   判据 SQL 与 GO-9 销案对拍 bit-identical:
   EXISTS (SELECT 1 FROM daily_klines k WHERE k.code=fd.code AND k.date=fd.date)
2. --skip-kline-backed 开关 (默认算; 给未来 apply 场景留快路, 不改默认行为)。
3. 施工 base=今晚窗后新 main (明早广播 hash), 09-05 22:05 窗。

RED 语义 (修复前必须红):
- R1: gap 循环拆出模块级函数 _gap_split_by_horizon(conn, horizon_cols)
     (main 内联→函数化, 为可测性; 对 main 是纯提取零逻辑变)。
- R2: 返回值三口径 {h: {"null": n, "kline_backed": k, "structural": n-k}}。
- R3: 判据 SQL 含 EXISTS daily_klines JOIN ON code+date (销案同源判据)。
- R4: argparse 有 --skip-kline-backed (默认 False=默认算)。
- R5: 跳过路径 kline_backed/structural 键存在但为 None (只增不减: 不算时
     也得占位, 消费方能区分「没算」vs「没这键」)。
"""
import argparse
import sqlite3

import pytest

from scripts import backfill_fwd_returns_history as bf


def _mk_conn():
    """最小三表假库: factor_daily(1列NULL) + daily_klines(可匹配/不可匹配各半)。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE factor_daily (code TEXT, date TEXT, "
        "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_20d REAL)")
    conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
    # 两行 NULL 5d: 一行有 kline 匹配 (可填), 一行无 (结构性)
    conn.execute("INSERT INTO factor_daily VALUES ('000001', '2026-07-01', NULL, NULL, NULL)")
    conn.execute("INSERT INTO factor_daily VALUES ('999999', '2026-07-01', NULL, NULL, NULL)")
    conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-07-01', 10.0)")
    conn.commit()
    return conn


class TestR1FunctionExtracted:
    def test_module_has_gap_split_fn(self):
        assert hasattr(bf, "_gap_split_by_horizon"), (
            "gap 循环须拆模块级函数 _gap_split_by_horizon (可测性, 纯提取)"
        )

    def test_signature_takes_conn_and_cols(self):
        fn = getattr(bf, "_gap_split_by_horizon", None)
        if fn is None:
            pytest.fail("RED: 函数未拆出")
        import inspect
        sig = inspect.signature(fn)
        assert "conn" in sig.parameters and "horizon_cols" in sig.parameters, (
            "签名须 (conn, horizon_cols) — 与 main 现场同形态"
        )


class TestR2ThreeCalibers:
    def test_returns_three_calibers(self):
        fn = getattr(bf, "_gap_split_by_horizon", None)
        if fn is None:
            pytest.fail("RED: 函数未拆出")
        conn = _mk_conn()
        cols = {1: "fwd_return_1d", 5: "fwd_return_5d", 20: "fwd_return_20d"}
        out = fn(conn, cols)
        assert out == {
            1: {"null": 2, "kline_backed": 1, "structural": 1},
            5: {"null": 2, "kline_backed": 1, "structural": 1},
            20: {"null": 2, "kline_backed": 1, "structural": 1},
        }, "两行 NULL (一行可填一行结构) → 三口径分明"


class TestR3CriterionSql:
    def test_source_contains_kline_exists_join(self):
        import inspect
        fn = getattr(bf, "_gap_split_by_horizon", None)
        if fn is None:
            pytest.fail("RED: 函数未拆出")
        src = inspect.getsource(fn)
        assert "EXISTS" in src.upper() and "daily_klines" in src, (
            "判据须 EXISTS daily_klines ON code+date (GO-9 销案对拍同源)"
        )


class TestR4SkipSwitch:
    def test_argparse_has_skip_kline_backed(self):
        parser = bf.build_arg_parser() if hasattr(bf, "build_arg_parser") else None
        if parser is None:
            # 回落: 现版 argparse 在 main() 内联 → RED (R4 一并要求拆 build_arg_parser)
            pytest.fail("RED: 须拆 build_arg_parser() 供 --skip-kline-backed 可测")
        args = parser.parse_args([])
        assert args.skip_kline_backed is False, "默认 False = 默认算"


class TestR5SkipPathPlaceholders:
    def test_skip_yields_placeholder_none(self):
        fn = getattr(bf, "_gap_split_by_horizon", None)
        if fn is None:
            pytest.fail("RED: 函数未拆出")
        conn = _mk_conn()
        cols = {5: "fwd_return_5d"}
        out = fn(conn, cols, skip_kline_backed=True)
        assert out[5]["kline_backed"] is None and out[5]["structural"] is None, (
            "跳过路径占位 None (只增不减: 区分「没算」vs「没键」)"
        )
        assert out[5]["null"] == 2, "null 口径不受开关影响恒算"
