# -*- coding: utf-8 -*-
"""跨边界钉: 走 KlinePipeline.run() 真执行路径 (判例 #150 三巡 — 统筹注入验证抓出
「测试只钉函数不钉接线」: 5 钉全绿但注掉 records 分支调用仍全绿)。

本钉必须: 走真实 run() → fake source 返「全 0 的一行记录」(6 只的真实形态)
→ 断言 suspended_codes 出现该 code。
验收: ①基线绿 ②注掉 records 分支调用 → 必须红 ③还原 → 绿。
"""
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

DATE = "2026-10-20"
DEGEN = "000016"      # 源返了记录但记录全 0 (records 分支形态)
NONZERO = "300001"    # 源返了非零真值行 (不该入表)


class _FakeKline:
    """最小 Kline 替身 (字段名对齐 pipelines/kline.py 的消费面)。"""
    def __init__(self, code, date, close, volume, amount):
        self.code = code
        self.date = date
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = volume
        self.amount = amount
        self.pre_close = close
        self.change_pct = 0.0
        self.turnover_rate = 0.0


class _FakeSource:
    """假源: get_data('kline', ...) 返指定形态的 1 行记录 (非空 ⇒ 走 records 分支)。"""
    name = "fake_degenerate_source"
    capabilities = {"kline"}

    def __init__(self, rows_by_code):
        self._rows = rows_by_code
        self.calls = []

    def get_data(self, data_type, params):
        self.calls.append((data_type, dict(params)))
        code = params.get("code")
        rows = self._rows.get(code)
        if not rows:
            return []
        return rows


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily_klines (
        code TEXT, date TEXT, volume REAL, amount REAL, close REAL,
        open REAL, high REAL, low REAL, pre_close REAL, change_pct REAL,
        turnover_rate REAL)""")
    conn.commit()
    return conn, path


def _registry(*sources):
    reg = MagicMock()
    reg.get_sources_for.side_effect = lambda dt: list(sources)
    return reg


class TestPipelineRunMarksDegenerate:
    """跨边界: 真跑 run(), 断言 records 分支会把退化票写进 suspended_codes。"""

    def test_records_branch_marks_degenerate_code(self):
        """正向: 源返「全 0 的一行记录」 ⇒ run() 后 suspended_codes 有该码。"""
        from data_layer.pipelines.kline import KlinePipeline
        from data_layer.pipelines.base import PipelineInput

        conn, path = _make_db()
        try:
            src = _FakeSource({
                DEGEN: [_FakeKline(DEGEN, DATE, 0.0, 0.0, 0.0)],
            })
            pipe = KlinePipeline(source_registry=_registry(src))
            inp = PipelineInput(date=DATE, db=MagicMock(get_connection=lambda: conn),
                                symbols=[DEGEN])
            with patch("data_layer.pipelines.kline.get_repo") as grepo:
                repo = MagicMock()
                repo.stock.get_all_active.return_value = [DEGEN]
                repo.kline.batch_upsert.side_effect = lambda rows: (
                    conn.executemany(
                        "INSERT OR REPLACE INTO daily_klines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [(r["code"], r["date"], r["volume"], r["amount"], r["close"],
                          r["open"], r["high"], r["low"], r["pre_close"],
                          r["change_pct"], r["turnover_rate"]) for r in rows]),
                    len(rows))[1]
                grepo.return_value = repo
                pipe.run(inp)
            n = conn.execute(
                "SELECT COUNT(*) FROM suspended_codes WHERE code=?", (DEGEN,)).fetchone()[0]
            assert n == 1, (
                "records 分支返全 0 记录 ⇒ 该码必须入 suspended_codes "
                "(跨边界钉: 注掉 records 分支的调用点本测试必须变红)")
        finally:
            conn.close(); os.unlink(path)

    def test_records_branch_nonzero_not_marked(self):
        """反向: 源返非零真值行 ⇒ 不入表 (判据不放宽, 跨边界同样守)。"""
        from data_layer.pipelines.kline import KlinePipeline
        from data_layer.pipelines.base import PipelineInput

        conn, path = _make_db()
        try:
            src = _FakeSource({
                NONZERO: [_FakeKline(NONZERO, DATE, 10.0, 100.0, 5000.0)],
            })
            pipe = KlinePipeline(source_registry=_registry(src))
            inp = PipelineInput(date=DATE, db=MagicMock(get_connection=lambda: conn),
                                symbols=[NONZERO])
            with patch("data_layer.pipelines.kline.get_repo") as grepo:
                repo = MagicMock()
                repo.stock.get_all_active.return_value = [NONZERO]
                repo.kline.batch_upsert.side_effect = lambda rows: len(rows)
                grepo.return_value = repo
                pipe.run(inp)
            # 表可能压根没建 (不该入表 ⇒ 不会建) — 查前先 ensure 再断言 0
            from data_layer.pipelines.kline import _ensure_suspended_table
            _ensure_suspended_table(conn)
            n = conn.execute(
                "SELECT COUNT(*) FROM suspended_codes WHERE code=?",
                (NONZERO,)).fetchone()[0]
            assert n == 0, "非零真值行 ⇒ 不入表"
        finally:
            conn.close(); os.unlink(path)

    def test_wiring_presence_is_guarded(self):
        """接线在位钉: 源码里 records 分支必须调 _maybe_mark_suspended
        (函数级钉之外再加一道「调用点存在」检查 — 判例 #150 的最低要求)。"""
        import inspect
        from data_layer.pipelines import kline as kline_mod
        src = inspect.getsource(kline_mod)
        # 调用点数量: err 分支 + records 分支 = 2 (形态随实现演进, 钉数量不钉字面串)
        call_sites = [l for l in src.splitlines()
                      if "_maybe_mark_suspended(conn, code_done" in l]
        assert len(call_sites) >= 2, (
            f"两个分支都应调用 _maybe_mark_suspended (err + records), "
            f"实到 {len(call_sites)}: {call_sites}")
        # records 分支那处必须带 records=records (判定源=本次返的记录,
        # 否则查库内末行看到的还是昨天的行 → 6 只漏检复发)
        assert any("records=records" in l for l in call_sites), (
            f"records 分支调用必须带 records=records, 实到: {call_sites}")
