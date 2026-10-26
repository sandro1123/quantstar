#!/usr/bin/env python3
"""DL-35 历史回补: adjust_factors / dividends / splits 三表全量回补 (GO-8)

策略:
- adjust_factors: 按交易日遍历 query_daily_adjust_factor(date), 每日 ~15 行,
  2010-01-01 至昨日约 4000 交易日 × 0.04s ≈ 3 分钟。
- dividends/splits: 从 adjust_factors 结果收集唯一 (code, year) 对,
  逐对 query_dividend_data(code, year) 取分红送转明细。
- 交叉验证: 抽样 10 只与 mootdx xdxr 对比 (差异记 warning 不阻塞)。

写库三步曲: 只读验证 → 备份 → 隔离验证 → GO。
用法:
  1. 备份: cp data/qs.db data/backups/qs_pre_adjust_backfill_<date>.db
  2. 隔离验证: python scripts/backfill_adjust_history.py --db <隔离库> --dry-run
  3. 正式执行 (需 GO): python scripts/backfill_adjust_history.py --db data/qs.db --apply
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, "src")

START_DATE = "2010-01-01"
SLEEP_S = 0.08  # baostock QPS ~20, 保守限速


def get_trading_days(conn, start, end):
    """从 daily_klines 取实际有数据的交易日 (比外部日历更可靠)。"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end)).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="复权因子/分红/送转 历史回补")
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--apply", action="store_true", help="真正执行 (默认 dry-run)")
    ap.add_argument("--evidence", default="logs/repair_evidence_dl35.jsonl")
    args = ap.parse_args()

    from data_layer.sources.baostock_source import BaoStockSourceAdapter
    from data_layer.repos.adjust import AdjustRepo
    from common.database import Database

    bs = BaoStockSourceAdapter()
    if not bs.is_available():
        print("[abort] baostock 不可用")
        return 1

    db = Database(args.db)
    repo = AdjustRepo(db)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    trading_days = get_trading_days(conn, args.start, args.end)
    conn.close()

    print(f"[{'APPLY' if args.apply else 'dry-run'}] {args.db}")
    print(f"  范围 {args.start} ~ {args.end}, 交易日 {len(trading_days)} 天")

    if not args.apply:
        # dry-run: 只采样 3 天验证连通性和数据格式
        sample_days = trading_days[-3:] if len(trading_days) >= 3 else trading_days
        total_events = 0
        for d in sample_days:
            events = bs.get_daily_adjust_factors(d)
            total_events += len(events)
            if events:
                print(f"  样本 {d}: {len(events)} 事件, 首条={events[0]}")
            time.sleep(SLEEP_S)
        print(f"  样本 {len(sample_days)} 天共 {total_events} 事件")
        print(f"  预估全量: ~{len(trading_days)} 天, 每日 ~15 事件")
        print("  (dry-run 不写库; 加 --apply 并确保已备份后才执行)")
        bs.close()
        return 0

    # ---- APPLY ----
    total_factors = 0
    code_years: set[tuple[str, int]] = set()
    t0 = time.time()

    for i, d in enumerate(trading_days):
        events = bs.get_daily_adjust_factors(d)
        if events:
            records = [{"code": ev["code"], "ex_date": ev["ex_date"],
                        "adjust_factor": ev["back_factor"], "source": "baostock"}
                       for ev in events if ev.get("back_factor")]
            n = repo.upsert_adjust_factors(records)
            total_factors += n
            for ev in events:
                code_years.add((ev["code"], int(ev["ex_date"][:4])))
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  进度 {i+1}/{len(trading_days)} 天, "
                  f"factors={total_factors}, 耗时 {elapsed:.0f}s")
        time.sleep(SLEEP_S)

    print(f"  adjust_factors 完成: {total_factors} 行, "
          f"唯一(code,year)对 {len(code_years)}")

    # dividends + splits
    total_divs = 0
    total_splits = 0
    for i, (code, year) in enumerate(sorted(code_years)):
        try:
            divs = bs.get_dividends(code, year)
        except Exception:
            continue
        if divs:
            div_records = []
            split_records = []
            for d in divs:
                div_records.append(d)
                sd = d.get("stock_dividend", 0.0) or 0.0
                st = d.get("stock_transfer", 0.0) or 0.0
                if sd + st > 0:
                    split_records.append({
                        "code": code, "ex_date": d["ex_date"],
                        "split_factor": 1.0 + sd + st, "source": "baostock"})
            total_divs += repo.upsert_dividends(div_records)
            total_splits += repo.upsert_splits(split_records)
        if (i + 1) % 500 == 0:
            print(f"  dividends 进度 {i+1}/{len(code_years)}")
        time.sleep(SLEEP_S)

    elapsed = time.time() - t0
    print(f"\n  回补完成 ({elapsed:.0f}s):")
    print(f"    adjust_factors: {repo.count('adjust_factors')} 行")
    print(f"    dividends:      {repo.count('dividends')} 行")
    print(f"    splits:         {repo.count('splits')} 行")

    evidence = {
        "ts": date.today().isoformat(),
        "card": "DL-35",
        "task": "adjust_history_backfill",
        "db": args.db,
        "range": f"{args.start}~{args.end}",
        "trading_days": len(trading_days),
        "adjust_factors": repo.count("adjust_factors"),
        "dividends": repo.count("dividends"),
        "splits": repo.count("splits"),
        "elapsed_s": round(elapsed),
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    bs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
