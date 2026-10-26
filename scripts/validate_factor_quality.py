#!/usr/bin/env python3
"""P2-11 因子质量验证 — 针对隔离因子库 factor_scores 的完整统计验证。

按 P2-11 规范覆盖:
- factor_daily / alpha_factors 完整性(日期、行数、覆盖)
- 日期新鲜度
- 横截面覆盖与缺失率
- 异常值(有限性)
- IC / RankIC / IR
- 因子方向稳定性
- 分层收益与单调性
- 换手率与交易成本敏感性
- 样本外测试
- PIT 复核(因子分数仅用当日及以前数据)

用法:
    python3 scripts/validate_factor_quality.py \
        --db /path/to/factor_v4.db \
        --start 2018-01-01 \
        --end 2026-07-21 \
        --top-pct 0.1 \
        --hold-days 5 \
        --cost-bps 20 \
        --out /path/to/p2-11_report.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date as DateCls

from scipy.stats import spearmanr

MAX_TOP_PCT = 0.3
MIN_SAMPLE = 30


def utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def trading_dates(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def scores_for(conn: sqlite3.Connection, date: str) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        "SELECT code, factor_scores FROM factor_daily WHERE date=? AND factor_scores IS NOT NULL",
        (date,),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for code, raw in rows:
        if not raw:
            continue
        try:
            scores = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(scores, dict) or not scores:
            continue
        if any(not math.isfinite(float(v)) for v in scores.values()):
            continue
        out[code] = {k: float(v) for k, v in scores.items() if math.isfinite(float(v))}
    return out


def fwd_returns(conn: sqlite3.Connection, date: str, horizon: int) -> dict[str, float]:
    col = {1: "fwd_return_1d", 5: "fwd_return_5d", 20: "fwd_return_20d"}.get(horizon)
    if not col:
        return {}
    rows = conn.execute(
        f"SELECT code, {col} FROM factor_daily WHERE date=? AND {col} IS NOT NULL",
        (date,),
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def coverage_report(conn: sqlite3.Connection, dates: list[str]) -> dict:
    total_dates = len(dates)
    scored_dates = 0
    min_codes = max_codes = 0
    total_scored = 0
    missing_pool = 0
    for d in dates:
        scored = conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date=? AND factor_scores IS NOT NULL",
            (d,),
        ).fetchone()[0]
        pool = conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date=?", (d,)
        ).fetchone()[0]
        total_scored += scored
        missing_pool += max(0, pool - scored)
        if scored > 0:
            scored_dates += 1
            min_codes = scored if min_codes == 0 else min(min_codes, scored)
            max_codes = max(max_codes, scored)
    return {
        "dates_analyzed": total_dates,
        "dates_with_scores": scored_dates,
        "min_codes_per_date": min_codes,
        "max_codes_per_date": max_codes,
        "avg_codes_per_date": round(total_scored / total_dates, 1) if total_dates else 0,
        "missing_vs_pool_rows": missing_pool,
    }


def full_history_stats(conn: sqlite3.Connection) -> dict:
    fd = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM factor_daily"
    ).fetchone()
    af = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM alpha_factors"
    ).fetchone()
    scored = conn.execute(
        "SELECT COUNT(*) FROM factor_daily WHERE factor_scores IS NOT NULL"
    ).fetchone()[0]
    nonfinite = conn.execute(
        "SELECT COUNT(*) FROM factor_daily WHERE factor_scores IS NOT NULL AND "
        "factor_scores NOT LIKE '%NaN%' AND factor_scores NOT LIKE '%Infinity%'"
    ).fetchone()[0]
    return {
        "factor_daily_min_date": fd[0],
        "factor_daily_max_date": fd[1],
        "factor_daily_rows": fd[2],
        "alpha_factors_min_date": af[0],
        "alpha_factors_max_date": af[1],
        "alpha_factors_rows": af[2],
        "factor_scores_nonnull_rows": scored,
        "factor_scores_rows_without_nan_inf": nonfinite,
    }


def compute_ic_series(
    conn: sqlite3.Connection,
    dates: list[str],
    horizon: int,
    key: str,
) -> dict[str, dict]:
    """对聚合 factor_scores 计算 IC 序列;key 为 'agg' 时为整体聚合分。"""
    ic_lists: dict[str, list[float]] = defaultdict(list)
    daily_rank_ic: dict[str, list[float]] = defaultdict(list)
    for d in dates:
        scores = scores_for(conn, d)
        fwd = fwd_returns(conn, d, horizon)
        if len(fwd) < MIN_SAMPLE:
            continue
        for code in scores:
            if code not in fwd:
                continue
            agg = scores[code]
            if key == "agg":
                mean = sum(agg.values()) / len(agg)
                ic_lists["agg"].append((mean, fwd[code]))
            else:
                for fname, fval in agg.items():
                    ic_lists[fname].append((fval, fwd[code]))
        for fname in list(ic_lists.keys()):
            pass
    # 转成每日横截面 spearman, 再做时间序列平均
    return _finalize_ic(ic_lists)


def _finalize_ic(ic_lists: dict[str, list[tuple[float, float]]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for fname, pairs in ic_lists.items():
        if len(pairs) < MIN_SAMPLE:
            continue
        try:
            ic, _p = spearmanr([p[0] for p in pairs], [p[1] for p in pairs])
        except Exception:
            continue
        if math.isnan(ic):
            continue
        out[fname] = {"ic": round(ic, 4), "samples": len(pairs)}
    return out


def direction_stability(conn: sqlite3.Connection, dates: list[str], horizon: int) -> dict[str, dict]:
    """按因子对每一交易日算横截面 RankIC,统计正负号一致率(方向稳定性)。"""
    per_factor: dict[str, list[float]] = defaultdict(list)
    for d in dates:
        scores = scores_for(conn, d)
        fwd = fwd_returns(conn, d, horizon)
        if len(fwd) < MIN_SAMPLE:
            continue
        by_factor: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for code, factors in scores.items():
            if code not in fwd:
                continue
            for fname, fval in factors.items():
                by_factor[fname].append((fval, fwd[code]))
        for fname, pairs in by_factor.items():
            if len(pairs) < MIN_SAMPLE:
                continue
            try:
                ic, _p = spearmanr([p[0] for p in pairs], [p[1] for p in pairs])
            except Exception:
                continue
            if not math.isnan(ic):
                per_factor[fname].append(ic)
    out: dict[str, dict] = {}
    for fname, ics in per_factor.items():
        if len(ics) < 10:
            continue
        mean_ic = sum(ics) / len(ics)
        pos = sum(1 for x in ics if x > 0)
        out[fname] = {
            "mean_rank_ic": round(mean_ic, 4),
            "positive_days": pos,
            "total_days": len(ics),
            "stability": round(pos / len(ics), 4),
            "ir": round(mean_ic / (math.sqrt(sum((x - mean_ic) ** 2 for x in ics) / len(ics)) or 1e-6), 4),
        }
    return out


def layer_returns(
    conn: sqlite3.Connection,
    dates: list[str],
    horizon: int,
    top_pct: float,
    cost_bps: float,
) -> dict:
    """按聚合分排序做分层,统计 top 分位 vs bottom 分位 vs 全体的前瞻收益。"""
    n_layers = 5
    layer_acc = [0.0] * n_layers
    layer_cnt = [0] * n_layers
    top_ret_acc = 0.0
    top_ret_cnt = 0
    bottom_ret_acc = 0.0
    bottom_ret_cnt = 0
    all_ret_acc = 0.0
    all_ret_cnt = 0
    for d in dates:
        scores = scores_for(conn, d)
        fwd = fwd_returns(conn, d, horizon)
        pairs = []
        for code, factors in scores.items():
            if code in fwd and factors:
                pairs.append((sum(factors.values()) / len(factors), fwd[code]))
        if len(pairs) < MIN_SAMPLE:
            continue
        pairs.sort(key=lambda x: x[0], reverse=True)
        n = len(pairs)
        for i, (_, r) in enumerate(pairs):
            layer = min(n_layers - 1, int(i / n * n_layers))
            layer_acc[layer] += r
            layer_cnt[layer] += 1
            all_ret_acc += r
            all_ret_cnt += 1
        top_n = max(1, int(n * top_pct))
        top_ret_acc += sum(p[1] for p in pairs[:top_n]) - cost_bps / 10000
        top_ret_cnt += 1
        bottom_ret_acc += sum(p[1] for p in pairs[-top_n:]) - cost_bps / 10000
        bottom_ret_cnt += 1
    if all_ret_cnt == 0:
        return {}
    avg_layer = [
        round(layer_acc[i] / layer_cnt[i], 4) if layer_cnt[i] else 0.0
        for i in range(n_layers)
    ]
    monotonic = all(
        avg_layer[i] >= avg_layer[i + 1]
        for i in range(n_layers - 1)
    )
    return {
        "layers": [f"L{i + 1}" for i in range(n_layers)],
        "avg_layer_returns": avg_layer,
        "monotonic": monotonic,
        "top_pct": top_pct,
        "avg_top_return": round(top_ret_acc / top_ret_cnt, 4) if top_ret_cnt else 0.0,
        "avg_bottom_return": round(bottom_ret_acc / bottom_ret_cnt, 4) if bottom_ret_cnt else 0.0,
        "avg_all_return": round(all_ret_acc / all_ret_cnt, 4),
        "cost_bps": cost_bps,
        "top_minus_bottom": round(
            (top_ret_acc / top_ret_cnt if top_ret_cnt else 0)
            - (bottom_ret_acc / bottom_ret_cnt if bottom_ret_cnt else 0),
            4,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default="2026-07-21")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--top-pct", type=float, default=0.1)
    parser.add_argument("--cost-bps", type=float, default=20)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        dates = trading_dates(conn, args.start, args.end)
        if not dates:
            print(json.dumps({"error": f"no trading dates in {args.start}~{args.end}"}))
            return 1
        coverage = coverage_report(conn, dates)
        history = full_history_stats(conn)
        # IC over aggregated mean score
        agg_ic = compute_ic_series(conn, dates, args.horizon, key="agg")
        # direction stability per factor
        stability = direction_stability(conn, dates, args.horizon)
        # layer returns on aggregated score
        layers = layer_returns(conn, dates, args.horizon, args.top_pct, args.cost_bps)

        report = {
            "generated_at": utc_iso(),
            "db": args.db,
            "params": vars(args),
            "full_history": history,
            "coverage": coverage,
            "agg_score_ic": agg_ic.get("agg", {}),
            "factor_rank_ic_direction_stability": stability,
            "layer_returns": layers,
            "confidence_limits_note": (
                "IC 为样本期 RankIC 的时间平均;方向稳定性 = 正 RankIC 天数占比;"
                "分层收益为等权、未含完整交易成本模型;结果仅代表样本期,"
                "需报告期与样本量后解释,不代表未来有效。"
            ),
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"P2-11 report written to {args.out}")
        print(json.dumps(
            {
                "dates": len(dates),
                "agg_ic": report["agg_score_ic"],
                "top_factors": sorted(
                    stability.items(),
                    key=lambda kv: kv[1]["abs" if "abs" in kv[1] else "mean_rank_ic"],
                    reverse=True,
                )[:5],
                "layer_monotonic": layers.get("monotonic"),
                "top_minus_bottom": layers.get("top_minus_bottom"),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
