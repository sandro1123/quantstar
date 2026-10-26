"""后台回补 stock_valuation 断档 — 用 AkShare stock_value_em(东财) 逐股全序列拉取,覆盖多缺期。

一次性拉每只股票全历史估值序列(2085天),提取断档交易日写入,避免 11 天×全市场重复拉取。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = os.path.expanduser("~/quantstar-factor-iso/logs")
os.makedirs(LOG_DIR, exist_ok=True)
EVENT_LOG = os.path.join(LOG_DIR, f"backfill_valuation_{RUN_ID}.jsonl")


def log(event, **fields):
    line = {"timestamp": datetime.now().isoformat(), "event": event, "run_id": RUN_ID, **fields}
    with open(EVENT_LOG, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {event} {fields}")


def main():
    import akshare as ak
    db_path = os.environ.get("QS_DB", os.path.join("data", "qs.db"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    start = "2026-07-03"
    end = "2026-08-06"
    kdates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date>=? AND date<=? ORDER BY date",
        (start, end),
    ).fetchall()]
    miss = []
    for d in kdates:
        v = conn.execute("SELECT COUNT(*) FROM stock_valuation WHERE date=?", (d,)).fetchone()[0]
        # 断档判定: 覆盖率明显不足(正常全市场~5500/天)
        if v < 4000:
            miss.append(d)
    log("target_dates", days=miss, days_count=len(miss))

    if not miss:
        log("no_gaps", detail="no missing valuation days")
        conn.close()
        return 0

    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM stock_info WHERE is_active=1").fetchall()]
    log("codes", total=len(codes))

    wrote = 0
    failed_codes = 0
    rate_note = "东财估值接口单股限流, 逐股拉取, 每次 sleep 0.3s"
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            df = ak.stock_value_em(symbol=code)
            if df is None or df.empty:
                continue
            cached = {}
            df["_d"] = df["数据日期"].astype(str).str.replace("-", "")
            date_to_row = {row["_d"]: row for _, row in df.iterrows()}
            for d in miss:
                target = d.replace("-", "")
                row = date_to_row.get(target)
                if row is None:
                    continue
                def _f(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
                pe = _f(row.get("PE(TTM)"))
                pb = _f(row.get("市净率"))
                ps = _f(row.get("市销率"))
                mc = _f(row.get("总市值"))
                shares = _f(row.get("总股本"))
                div = _f(row.get("股息率"))
                conn.execute(
                    "INSERT OR REPLACE INTO stock_valuation "
                    "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (code, d, pe, pb, ps, mc, shares, div),
                )
                wrote += 1
            if i % 100 == 0:
                conn.commit()
                log("progress", codes_done=i, codes_total=len(codes),
                    wrote=wrote, elapsed_s=round(time.time() - t0, 1))
        except Exception:
            failed_codes += 1
        time.sleep(0.3)

    conn.commit()
    log("done", wrote=wrote, failed_codes=failed_codes,
        elapsed_s=round(time.time() - t0, 1))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())