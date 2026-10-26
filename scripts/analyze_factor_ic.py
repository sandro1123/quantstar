#!/usr/bin/env python3
"""Factor IC Analysis — 计算所有因子的 IC (Information Coefficient)"""

import os
import sys
import math
import json
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.expanduser("~/qs/src"))

from common.database import Database
from data_layer._loader import DataLoader
from strategy_layer.engine import FactorEngine
from strategy_layer.factors import ALL_FACTORS
from strategy_layer.factors.factor_cleaning import mad_clip, zscore
from scipy.stats import spearmanr


def get_trading_dates(db, end_date: str, n_days: int = 60) -> list[str]:
    """获取最近 N 个交易日"""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (end_date, n_days),
    ).fetchall()
    return sorted([r[0] for r in rows])


def get_forward_returns(db, date: str, horizon: int = 5) -> dict[str, float]:
    """获取某日所有股票的 N 日前瞻收益率"""
    conn = db.get_connection()
    future_date_row = conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date > ? ORDER BY date LIMIT 1 OFFSET ?",
        (date, horizon - 1),
    ).fetchone()
    if not future_date_row:
        return {}
    future_date = future_date_row[0]

    current = {}
    for r in conn.execute(
        "SELECT code, close FROM daily_klines WHERE date = ?", (date,)
    ).fetchall():
        if r["close"] and r["close"] > 0:
            current[r["code"]] = r["close"]

    future = {}
    for r in conn.execute(
        "SELECT code, close FROM daily_klines WHERE date = ?", (future_date,)
    ).fetchall():
        if r["close"] and r["close"] > 0:
            future[r["code"]] = r["close"]

    returns = {}
    for code in current:
        if code in future and current[code] > 0:
            returns[code] = (future[code] - current[code]) / current[code]
    return returns


def main():
    db = Database(os.path.expanduser("~/qs/data/qs.db"))
    loader = DataLoader(db)
    engine = FactorEngine(data_layer=loader)

    end_date = "2026-07-17"
    n_days = 20
    dates = get_trading_dates(db, end_date, n_days)
    print(f"Analyzing {len(dates)} trading days: {dates[0]} ~ {dates[-1]}")

    factor_ic_sum = defaultdict(float)
    factor_ic_count = defaultdict(int)
    factor_ic_list = defaultdict(list)

    for i, date in enumerate(dates):
        if i % 10 == 0:
            print(f"  Processing {date} ({i+1}/{len(dates)})...")

        conn = db.get_connection()
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_klines WHERE date = ? "
            "AND code IN (SELECT code FROM factor_daily WHERE date = ? AND fwd_return_5d IS NOT NULL)",
            (date, date),
        ).fetchall()]

        if len(codes) < 50:
            continue

        codes = codes[:200]

        fwd_returns_raw = {}
        for r in conn.execute(
            "SELECT code, fwd_return_5d FROM factor_daily WHERE date = ? AND fwd_return_5d IS NOT NULL",
            (date,),
        ).fetchall():
            if r[1] is not None:
                fwd_returns_raw[r[0]] = r[1]

        fwd_returns = {c: fwd_returns_raw[c] for c in codes if c in fwd_returns_raw}
        if len(fwd_returns) < 30:
            continue

        try:
            matrix = engine.compute_all(codes, date)
        except Exception as e:
            print(f"  Error computing factors for {date}: {e}")
            continue

        factor_scores = defaultdict(dict)
        for code, scores in matrix.items():
            for fname, score in scores.items():
                if code in fwd_returns:
                    factor_scores[fname][code] = score

        for fname, scores in factor_scores.items():
            codes_list = list(scores.keys())
            score_vals = [scores[c] for c in codes_list]
            return_vals = [fwd_returns[c] for c in codes_list]

            if len(score_vals) < 20 or len(set(score_vals)) < 3:
                continue

            try:
                ic, p_value = spearmanr(score_vals, return_vals)
                if not math.isnan(ic):
                    factor_ic_sum[fname] += ic
                    factor_ic_count[fname] += 1
                    factor_ic_list[fname].append(ic)
            except Exception:
                pass

    print("\n" + "=" * 80)
    print("FACTOR IC ANALYSIS RESULTS (5-day forward return, Spearman rank correlation)")
    print("=" * 80)
    print(f"{'Factor':<30} {'Mean IC':>8} {'|IC|':>8} {'IC Std':>8} {'IR':>8} {'Days':>6} {'Sig?':>6}")
    print("-" * 80)

    results = []
    for fname in factor_ic_sum:
        count = factor_ic_count[fname]
        if count < 3:
            continue
        mean_ic = factor_ic_sum[fname] / count
        ic_values = factor_ic_list[fname]
        ic_std = math.sqrt(sum((x - mean_ic) ** 2 for x in ic_values) / len(ic_values)) if len(ic_values) > 1 else 0.001
        ir = mean_ic / ic_std if ic_std > 0 else 0
        abs_ic = abs(mean_ic)
        significant = abs_ic > 0.02 and count >= 5
        results.append({
            "factor": fname,
            "mean_ic": round(mean_ic, 4),
            "abs_ic": round(abs_ic, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4),
            "days": count,
            "significant": significant,
        })

    results.sort(key=lambda x: x["abs_ic"], reverse=True)

    for r in results:
        sig = "***" if r["significant"] else ""
        print(f"{r['factor']:<30} {r['mean_ic']:>8.4f} {r['abs_ic']:>8.4f} {r['ic_std']:>8.4f} {r['ir']:>8.4f} {r['days']:>6} {sig:>6}")

    with open(os.path.expanduser("~/qs/data/factor_ic_results.json"), "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to data/factor_ic_results.json")

    sig_factors = [r for r in results if r["significant"]]
    print(f"\nSignificant factors (|IC| > 0.02): {len(sig_factors)}")
    for r in sig_factors:
        direction = "↑" if r["mean_ic"] > 0 else "↓"
        print(f"  {r['factor']}: IC={r['mean_ic']:+.4f} {direction}")


if __name__ == "__main__":
    main()
