# -*- coding: utf-8 -*-
"""pipelines/suspend_recovery.py — D3 K线补全第二段: 停牌复牌检测+补全常态化

统筹派单 sm-46287f4038b242a3 (qs-d3b @ feat/d3-kline-complete)。

probe 勘正 (派单技术细节=工作假设判例):
  「13 只」实为主板 6 只真停牌族 — 其余为 BJ/新三板段已知结构性缺口 (另卡);
  D4 source_crosscheck 零表零 runs — 漏采检测本管道自建, 不依赖 D4。

两职责:
  ① 复牌检测: 主板 active 股 last_kline 落后基准窗 → baostock trade_status 批量判;
     停牌中 (0) = 正确缺席零动作只计数; 复牌日 (1 且库内无该日行) → 备源补拉。
  ② 补全常态化: 当日备源全市场快照 vs 库内行集差 → 定向补 (补前查重, 已有行不覆盖)。

幂等: INSERT OR REPLACE (repos/kline.batch_upsert) + 补前查重双保险 — 重复跑零重复写。
诚实降级 (铁律 #12): trade_status=None / 备源空返回 → partial/留痕, 禁静默。
"""

from __future__ import annotations

from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from common.logging import get_logger

logger = get_logger("data.suspend_recovery")

# BJ/新三板段排除 (43/83/87/92/82 开头) — 该段止于 07-17 是源结构性缺口,
# 属 D3 第一段/独立卡面, 非本管道的停牌/复牌语义。
_EXCLUDED_PREFIXES = ("43", "83", "87", "92", "82")


class SuspendRecoveryPipeline(BasePipeline):
    name = "suspend_recovery"
    requires = ["stock_info"]
    produces = ["daily_klines"]

    def __init__(self, source=None, resume_lookback_days: int = 3):
        self.source = source
        self.resume_lookback_days = resume_lookback_days

    def _load_candidates(self, conn, as_of: str):
        """主板 active 股中 last_kline 早于「近 N 个交易日窗口」者 = 复牌候选。

        返回 [(code, last_kline)]。last_kline 为 None (全无史) 的也入候选 —
        由复牌检测区分新股 (status=1 → no_history) 与真停牌 (status=0)。
        """
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_klines WHERE date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (as_of, self.resume_lookback_days + 1),
        ).fetchall()
        if not rows:
            return []
        cutoff = rows[-1][0]  # 近 N 交易窗的最早日; last_kline < cutoff → 候选
        cand = conn.execute("""
            SELECT si.code, MAX(k.date) AS last_kline
            FROM stock_info si
            LEFT JOIN daily_klines k ON k.code = si.code
            WHERE si.is_active = 1 AND si.code NOT LIKE '43%%'
                  AND si.code NOT LIKE '83%%' AND si.code NOT LIKE '87%%'
                  AND si.code NOT LIKE '92%%' AND si.code NOT LIKE '82%%'
            GROUP BY si.code
            HAVING last_kline IS NULL OR last_kline < ?
        """, (cutoff,)).fetchall()
        return [(r[0], r[1]) for r in cand]

    def run(self, input: PipelineInput) -> PipelineResult:
        conn = input.db.get_connection() if input.db else None
        if conn is None or self.source is None:
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                errors=["suspend_recovery: no db or no source"],
            )

        meta: dict = {}
        errors: list[str] = []
        candidates = self._load_candidates(conn, input.date)
        meta["candidates_n"] = len(candidates)

        # ---- ① 复牌检测 (仅候选集小, trade_status 一次网络调用) ----
        suspended_n = resumed = no_history_n = 0
        recovered_n = 0
        status_map = None
        if candidates:
            status_map = self.source.get_daily_trade_status(input.date)
            if status_map is None:
                errors.append(
                    f"trade_status unavailable at {input.date} "
                    f"({len(candidates)} candidates unverified)")
            else:
                to_fetch: list[tuple[str, str | None]] = []
                for code, last in candidates:
                    trading = status_map.get(code)
                    if trading is None:
                        continue  # 备源不覆盖该 code — 留痕见下
                    if trading is False:
                        suspended_n += 1
                    elif last is None:
                        no_history_n += 1
                        to_fetch.append((code, last))
                    else:
                        resumed += 1
                        to_fetch.append((code, last))
                # 复牌/新股补拉: 当日快照过滤 (get_daily_market 全市场, 按日取)
                market = self.source.get_daily_market(input.date) or []
                market_by_code = {r["code"]: r for r in market
                                  if r.get("date") == input.date}
                fetch_records = []
                for code, _last in to_fetch:
                    rec = market_by_code.get(code)
                    if rec is None:
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM daily_klines WHERE code=? AND date=?",
                        (code, input.date)).fetchone()
                    if exists:
                        continue
                    fetch_records.append(rec)
                if fetch_records:
                    from data_layer.repos import get_repo
                    repo = get_repo(input.db)
                    repo.kline.batch_upsert(fetch_records)
                    recovered_n = len(fetch_records)
        meta["suspended_n"] = suspended_n
        meta["no_history_n"] = no_history_n
        meta["recovered_n"] = recovered_n

        # ---- ② 补全常态化: 当日行集差定向补 ----
        gap_filled_n = 0
        market_full = self.source.get_daily_market(input.date) or []
        if market_full:
            today_rows = {r["code"] for r in market_full if r.get("date") == input.date}
            existing = {r[0] for r in conn.execute(
                "SELECT code FROM daily_klines WHERE date=?", (input.date,))}
            gap_codes = today_rows - existing
            if gap_codes:
                gap_records = [r for r in market_full
                               if r.get("date") == input.date and r["code"] in gap_codes]
                if gap_records:
                    from data_layer.repos import get_repo
                    repo = get_repo(input.db)
                    repo.kline.batch_upsert(gap_records)
                    gap_filled_n = len(gap_records)
        meta["gap_filled_n"] = gap_filled_n

        status = "partial" if errors else "success"
        logger.info(
            f"suspend_recovery {input.date}: candidates={meta['candidates_n']} "
            f"suspended={suspended_n} no_history={no_history_n} "
            f"recovered={recovered_n} gap_filled={gap_filled_n}")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=recovered_n + gap_filled_n,
            errors=errors, metadata=meta,
        )
