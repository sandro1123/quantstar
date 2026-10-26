# -*- coding: utf-8 -*-
"""report_layer/track_record.py — REC 模块3: 推荐战绩沉淀 (对用户可读, 带基准)。

统筹卡断点③: retrospect_settlement 有结算 (hit_5d/10d + AG-01 判定点
agent_layer.quality.evaluator.is_hit_ag01), 但没拧成对用户带基准的可读战绩
(有结算无呈现)。本模块做呈现层聚合, 不动结算核心 (单一定义点)。

基准 (统筹卡): 同期沪深300 (index_daily 000300.SH, rec_date→T+5 指数收益)
+ 随机50%基线 (无方向主张瞎推的先验)。超额 = 平均 fwd_return − 基准收益。
诚实标注: n<30 标「样本不足」(生产现实 verdict=推荐 n=1); 零样本 win_rate
=None 不造假 (census_guard 判例); 失败数 miss_count 如实呈现 (对失败诚实)。
"""
from __future__ import annotations

import sqlite3

from common.logging import get_logger

logger = get_logger("report.track_record")

BENCHMARK_CODE = "000300.SH"      # 沪深300
RANDOM_BASELINE = 0.5             # 无方向主张瞎推的先验命中率
ADEQUATE_SAMPLE = 30              # n<30 标样本不足 (统筹卡)


def _hs300_return_between(conn: sqlite3.Connection, rec_date: str, horizon_days: int = 5) -> float | None:
    """基准收益: 指数 rec_date 当日 (或其前最近) 收盘 → 之后第 horizon_days
    个交易日收盘。指数无行 (断档) → None (不造基准)。"""
    row = conn.execute(
        "SELECT close FROM index_daily WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (BENCHMARK_CODE, rec_date)).fetchone()
    if not row:
        return None
    base = row[0]
    after = conn.execute(
        "SELECT close FROM index_daily WHERE code=? AND date>? ORDER BY date ASC LIMIT ?",
        (BENCHMARK_CODE, rec_date, horizon_days)).fetchall()
    if len(after) < horizon_days:
        return None
    end = after[-1][0]
    if not base:
        return None
    return end / base - 1.0


def build_track_record(
    conn: sqlite3.Connection,
    verdict: str = "推荐",
    asof: str | None = None,
    horizon: str = "5d",
) -> dict:
    """聚合 verdict 的用户可读战绩。零样本诚实形态; n<30 标样本不足。

    返回: {sample_size, win_rate, miss_count, avg_return, benchmark_return,
           excess_return, random_baseline, sample_adequate, sample_note}
    """
    fwd_col = f"fwd_return_{horizon}"
    hit_col = f"hit_{horizon}"  # 列名就是 hit_5d/hit_10d (rstrip 会错成 hit_5)
    where = "WHERE verdict = ? AND settled_at IS NOT NULL"
    params: list = [verdict]
    if asof:
        where += " AND rec_date <= ?"
        params.append(asof)

    n = conn.execute(
        f"SELECT COUNT(*) FROM recommendation_retrospect {where} "
        f"AND {hit_col} IS NOT NULL", params).fetchone()[0]

    summary: dict = {
        "verdict": verdict,
        "horizon": horizon,
        "sample_size": n,
        "random_baseline": RANDOM_BASELINE,
    }

    if n == 0:
        # census_guard 判例: 空集显式 — 零样本 win_rate=None 不造 0%
        summary.update({
            "win_rate": None, "miss_count": 0, "avg_return": None,
            "benchmark_return": None, "excess_return": None,
            "sample_adequate": False, "sample_note": f"无已结算的「{verdict}」样本",
        })
        return summary

    wins = conn.execute(
        f"SELECT COUNT(*) FROM recommendation_retrospect {where} "
        f"AND {hit_col} = 1", params).fetchone()[0]
    miss = n - wins
    avg_row = conn.execute(
        f"SELECT AVG({fwd_col}) FROM recommendation_retrospect {where} "
        f"AND {fwd_col} IS NOT NULL", params).fetchone()
    avg_ret = avg_row[0] if avg_row else None

    # 基准: 用样本日期中位数的同期指数收益 (多点取样均值, 单日锚易被断档吃掉)
    dates = [r[0] for r in conn.execute(
        f"SELECT DISTINCT rec_date FROM recommendation_retrospect {where} "
        f"ORDER BY rec_date", params).fetchall()]
    bench_vals = []
    for d in dates:
        v = _hs300_return_between(conn, d, horizon_days=int(horizon.rstrip("d")))
        if v is not None:
            bench_vals.append(v)
    bench = sum(bench_vals) / len(bench_vals) if bench_vals else None
    excess = (avg_ret - bench) if (avg_ret is not None and bench is not None) else None

    adequate = n >= ADEQUATE_SAMPLE
    summary.update({
        "win_rate": wins / n,
        "miss_count": miss,
        "avg_return": avg_ret,
        "benchmark_return": bench,
        "excess_return": excess,
        "sample_adequate": adequate,
        "sample_note": ("" if adequate
                        else f"样本不足 (n={n} < {ADEQUATE_SAMPLE}), 战绩统计意义有限"),
    })
    return summary
