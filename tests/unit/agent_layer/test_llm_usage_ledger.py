# -*- coding: utf-8 -*-
"""留痕腿 TDD — LLM token 用量 write-through 落库（统筹审过 sm-fa3ca313515f4036）

■ 侦察出的决定性结构事实（本设计的根）：
  TokenBudget 是 ProviderManager 的**进程内实例**，而 crontab 45 条 pipeline
  **每次新起进程** → 进程退出，_usage 即消失。故「日终读 get_all_usage 落库」
  根本行不通（日终那个进程里内存早空）。**正解 = write-through**：每次记账即落库。

■ 五问裁定（统筹）：
  ① 新表 llm_token_usage（v11），列 (date, call_point, bucket, tokens, source, pid)
     保留期先不定（观察窗 60 行/天）；**表结构一次到位，免得 v11 后 v12 加列**
  ② 写点 = provider_manager call() 记账处；无幂等键（流水账，double-count 正确）；
     失败不炸主链 —— **但失败必须可观测**（warning 带 call_point+tokens，不许吞 debug）
  ③ 读谁：write-through 自动消解跨进程问题；多 worker SQL 聚合天然合并
  ④ 成本：独立短连接 + 失败即弃，不与主链共用长事务（flock 饿死案刚过）；
     **TDD 须有隔离性测试**
  ⑤ 缺失日判据 = 看「够不够」而非「有没有」（同心跳「连续 N 天」族）
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_layer.llm.usage_ledger import (
    USAGE_LEDGER_DDL, ensure_ledger_table, record_usage_row,
    summarize_day, EXPECTED_DAILY_CALLS, MIN_COVERAGE_RATIO,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


class TestSchema:
    """① 表结构一次到位（八列全建，免 v11 后 v12 加列）"""

    def test_all_columns_present(self, conn):
        ensure_ledger_table(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_token_usage)")}
        for c in ("id", "date", "call_point", "bucket", "tokens", "source",
                  "pid", "created_at"):
            assert c in cols, f"缺列 {c}"

    def test_index_on_date_bucket(self, conn):
        ensure_ledger_table(conn)
        names = [r[1] for r in conn.execute("PRAGMA index_list(llm_token_usage)")]
        assert any("date" in n for n in names), "缺 (date,bucket) 索引"

    def test_idempotent(self, conn):
        ensure_ledger_table(conn)
        ensure_ledger_table(conn)  # 二次不炸


class TestWriteThrough:
    """② 写点：每次记账落一行；流水账无幂等键（double-count 正确）"""

    def test_records_one_row(self, conn):
        ensure_ledger_table(conn)
        n = record_usage_row(conn, date="2026-09-25", call_point="expert:tech",
                             tokens=137, source="real")
        assert n == 1
        row = conn.execute("SELECT date, call_point, bucket, tokens, source "
                           "FROM llm_token_usage").fetchone()
        assert row[0] == "2026-09-25" and row[1] == "expert:tech"
        assert row[2] == "expert_debate"     # 归桶与 TokenBudget._bucket 同口径
        assert row[3] == 137 and row[4] == "real"

    def test_double_count_is_correct(self, conn):
        """同一 call_point 两次调用 → 两行（流水账，不去重）"""
        ensure_ledger_table(conn)
        record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=100)
        record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=100)
        n = conn.execute("SELECT COUNT(*) FROM llm_token_usage").fetchone()[0]
        assert n == 2, "流水账不去重 —— 重跑即真的又调了一次"

    def test_bucket_matches_token_budget(self, conn):
        """归桶口径与 TASK_BUDGETS 的 _bucket 一致（防两处口径漂移）"""
        from agent_layer.llm.token_budget import TokenBudget
        ensure_ledger_table(conn)
        for cp in ("expert:tech", "multi_agent_review:sector", "chart_reader"):
            record_usage_row(conn, date="2026-09-25", call_point=cp, tokens=10)
            got = conn.execute("SELECT bucket FROM llm_token_usage WHERE call_point=?",
                               (cp,)).fetchone()[0]
            assert got == TokenBudget._bucket(cp), f"{cp} 归桶口径漂移"

    def test_pid_recorded(self, conn):
        """pid 落库（多 worker 归因用）"""
        ensure_ledger_table(conn)
        record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=1)
        pid = conn.execute("SELECT pid FROM llm_token_usage").fetchone()[0]
        assert isinstance(pid, int) and pid > 0


class TestFailureIsolatedButVisible:
    """②④ 失败不炸主链，但必须可观测；隔离性测试"""

    def test_write_failure_returns_zero_not_raise(self):
        """conn 拒绝写入 → 返回 0，绝不抛（主链优先）"""
        class BadConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
        assert record_usage_row(BadConn(), date="2026-09-25",
                                call_point="expert:tech", tokens=5) == 0

    def test_write_failure_logs_warning_with_context(self, caplog):
        """失败必须 warning 且带 call_point+tokens（统筹加钉：不许吞成 debug）"""
        import logging
        class BadConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
        with caplog.at_level(logging.WARNING, logger="agent.llm.usage_ledger"):
            record_usage_row(BadConn(), date="2026-09-25",
                             call_point="expert:risk", tokens=42)
        msgs = " ".join(r.message for r in caplog.records)
        assert "expert:risk" in msgs and "42" in msgs, \
            f"失败 warning 须带 call_point+tokens，实得: {msgs!r}"

    def test_main_chain_unaffected_by_ledger_failure(self, conn):
        """隔离性: 留痕写失败不影响主链（另开连接写别的表仍成功）"""
        class BadConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("locked")
        record_usage_row(BadConn(), date="2026-09-25", call_point="x", tokens=1)
        conn.execute("CREATE TABLE main_chain (v INT)")
        conn.execute("INSERT INTO main_chain VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM main_chain").fetchone()[0] == 1


class TestCoverageJudgement:
    """⑤ 缺失日判据 = 看「够不够」而非「有没有」（同心跳族）"""

    def test_zero_rows_is_insufficient(self, conn):
        ensure_ledger_table(conn)
        rep = summarize_day(conn, "2026-09-25")
        assert rep["rows"] == 0
        assert rep["sufficient"] is False
        assert rep["ratio"] == 0.0

    def test_few_rows_insufficient_even_if_nonzero(self, conn):
        """有行但远不够 → 仍判不足（治「有就不管」的假绿）"""
        ensure_ledger_table(conn)
        for _ in range(3):
            record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=10)
        rep = summarize_day(conn, "2026-09-25")
        assert rep["rows"] == 3
        assert rep["sufficient"] is False, "有行≠够用 —— 判据看够不够"

    def test_full_coverage_is_sufficient(self, conn):
        ensure_ledger_table(conn)
        need = int(EXPECTED_DAILY_CALLS * MIN_COVERAGE_RATIO) + 1
        for i in range(need):
            record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=10)
        rep = summarize_day(conn, "2026-09-25")
        assert rep["sufficient"] is True
        assert rep["ratio"] >= MIN_COVERAGE_RATIO

    def test_report_has_buckets_breakdown(self, conn):
        ensure_ledger_table(conn)
        record_usage_row(conn, date="2026-09-25", call_point="expert:tech", tokens=100)
        record_usage_row(conn, date="2026-09-25", call_point="multi_agent_review:x", tokens=50)
        rep = summarize_day(conn, "2026-09-25")
        assert rep["by_bucket"]["expert_debate"] == 100
        assert rep["by_bucket"]["multi_agent_review"] == 50
        assert rep["total_tokens"] == 150

    def test_absent_day_not_confused_with_partial(self, conn):
        """缺失日 vs 有数据日可区分（空集与零命中必须可区分）"""
        ensure_ledger_table(conn)
        rep = summarize_day(conn, "2026-01-01")
        assert rep["status"] == "absent"
