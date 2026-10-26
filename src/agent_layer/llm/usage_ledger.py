# -*- coding: utf-8 -*-
"""LLM token 用量留痕 — write-through 落库（统筹审过 sm-fa3ca313515f4036）

■ 为什么是 write-through 而不是「日终读内存」：
  TokenBudget 是 ProviderManager 的**进程内实例**，而 crontab 45 条 pipeline
  **每次新起进程**（`python -m src pipeline run X`）→ 进程退出 _usage 即消失。
  「日终读 get_all_usage 落库」在该结构下**根本行不通**（日终进程内存早空）。
  正解 = 每次记账即落库，读侧退化为一条 SQL；gunicorn 多 worker 各写各行，
  SQL 聚合天然合并，无需跨进程汇总。

■ 五问落地（统筹裁定）：
  ① 表 llm_token_usage（v11）：列一次到位（八列），免 v11 后 v12 加列
  ② 写点 = provider_manager.call() 记账处；**无幂等键**（流水账，重跑=真的又调了一次，
     double-count 正确）；失败不炸主链，**但失败必须可观测**（warning 带上下文）
  ③ 读侧 = summarize_day()（SQL 聚合）
  ④ 隔离 = 独立短连接 + 失败即弃，不与主链共用长事务（flock 饿死案刚过）
  ⑤ 判据 = 看「够不够」而非「有没有」（同心跳「连续 N 天」族）
"""
import os

from common.logging import get_logger

logger = get_logger("agent.llm.usage_ledger")

# 表结构一次到位（统筹：免 v11 后 v12 加列，迁移能少一次是一次）
USAGE_LEDGER_DDL = """CREATE TABLE IF NOT EXISTS llm_token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    call_point TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
)"""

USAGE_LEDGER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ltu_date_bucket ON llm_token_usage (date, bucket)",
    "CREATE INDEX IF NOT EXISTS idx_ltu_call_point ON llm_token_usage (call_point, date)",
)

# ⑤ 覆盖判据：辩论全覆盖 = deep_top_n(10) × 6 专家 = 60 次/天
EXPECTED_DAILY_CALLS = 60
MIN_COVERAGE_RATIO = 0.5     # 低于半数视为「不够」→ 判不足


def ensure_ledger_table(conn) -> None:
    """幂等建表+索引。"""
    if conn is None:
        return
    conn.execute(USAGE_LEDGER_DDL)
    for ddl in USAGE_LEDGER_INDEXES:
        conn.execute(ddl)
    conn.commit()


def _bucket_of(call_point: str) -> str:
    """归桶口径**复用 TokenBudget._bucket**（单一定义点，防两处漂移）。"""
    try:
        from agent_layer.llm.token_budget import TokenBudget
        return TokenBudget._bucket(call_point)
    except Exception:
        return call_point or ""


def record_usage_row(conn, date: str, call_point: str, tokens: int,
                     source: str = "") -> int:
    """写一行用量流水。返回 1=成功，0=失败（**绝不抛**，主链优先）。

    失败必须**可观测**：warning 带 call_point/tokens（统筹加钉 ——
    「失败不炸」不等于「失败不记」，吞成 debug 等于没记）。
    """
    if conn is None:
        return 0
    try:
        conn.execute(
            "INSERT INTO llm_token_usage (date, call_point, bucket, tokens, source, pid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, call_point, _bucket_of(call_point), int(tokens or 0),
             source or "", os.getpid()))
        conn.commit()
        return 1
    except Exception as e:                    # noqa: BLE001 — 铁律#12：绝不炸主链
        logger.warning(f"usage_ledger 写入失败(不阻塞主链): call_point={call_point} "
                       f"tokens={tokens} date={date}: {e}")
        return 0


def summarize_day(conn, date: str) -> dict:
    """日汇总（读侧）。返回:
      status: absent(当日零行) / partial / covered
      rows / total_tokens / by_bucket / ratio / sufficient
    ⑤ 判据看「够不够」: rows/EXPECTED 达 MIN_COVERAGE_RATIO 才算 sufficient。
    """
    out = {"date": date, "rows": 0, "total_tokens": 0, "by_bucket": {},
           "ratio": 0.0, "sufficient": False, "status": "absent"}
    if conn is None:
        return out
    try:
        rows = conn.execute(
            "SELECT bucket, COUNT(*) c, COALESCE(SUM(tokens),0) t "
            "FROM llm_token_usage WHERE date=? GROUP BY bucket", (date,)).fetchall()
    except Exception as e:                    # noqa: BLE001
        logger.warning(f"usage_ledger 汇总失败: date={date}: {e}")
        return out
    if not rows:
        return out                            # absent（与 partial 可区分）
    total_rows = total_tokens = 0
    by_bucket: dict = {}
    for r in rows:
        bucket = r[0] if not hasattr(r, "keys") else r["bucket"]
        c = r[1] if not hasattr(r, "keys") else r["c"]
        t = r[2] if not hasattr(r, "keys") else r["t"]
        total_rows += c
        total_tokens += t
        by_bucket[bucket] = t
    ratio = total_rows / EXPECTED_DAILY_CALLS if EXPECTED_DAILY_CALLS else 0.0
    out.update({
        "rows": total_rows, "total_tokens": total_tokens, "by_bucket": by_bucket,
        "ratio": ratio, "sufficient": ratio >= MIN_COVERAGE_RATIO,
        "status": "covered" if ratio >= MIN_COVERAGE_RATIO else "partial",
    })
    return out
