"""后台: moneyflow 日期统一为 ISO + 回补断档(用 AkShare stock_individual_fund_flow)。

1. 将 stock_moneyflow 全部 compact 日期(YYYYMMDD) 规范化为主 ISO(YYYY-MM-DD) — 用户已授权。
2. 回补 11 个断档交易日(2026-07-22 ~ 2026-08-05) 全市场资金流。
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
EVENT_LOG = os.path.join(LOG_DIR, f"backfill_moneyflow_{RUN_ID}.jsonl")


def _market_of(code: str) -> str:
    """按上证/深证/北交所 代码前缀返回东财 market 参数。"""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    return "bj"  # 4/8 开头的北交所代码, 92 为 sh 债券忽略


def log(event, **fields):
    line = {"timestamp": datetime.now().isoformat(), "event": event, "run_id": RUN_ID, **fields}
    with open(EVENT_LOG, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {event} {fields}")


def main():
    import akshare as ak
    db_path = os.environ.get("QS_DB", os.path.expanduser("~/qs/data/qs.db"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ---- Step 1: normalize compact -> ISO ----
    rows = conn.execute(
        "SELECT code, date FROM stock_moneyflow WHERE date NOT LIKE '%-%-%'"
    ).fetchall()
    log("normalize_start", rows=len(rows))
    n_iso = 0
    for r in rows:
        d = r["date"]
        compact = d.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            iso = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
            conn.execute(
                "UPDATE stock_moneyflow SET date=? WHERE code=? AND date=?",
                (iso, r["code"], d),
            )
            n_iso += 1
    conn.commit()
    log("normalize_done", converted=n_iso)

    # ---- Step 2: backfill missing trading days ----
    start = "2026-07-22"
    end = "2026-08-07"
    kdates = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date>=? AND date<=?", (start, end)
    ).fetchall()}
    existing = {r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_moneyflow"
    ).fetchall()}
    miss = sorted(d for d in kdates if d not in existing)
    log("target_dates", days=miss, days_count=len(miss))
    if not miss:
        log("no_gaps")
        conn.close()
        return 0

    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM stock_info WHERE is_active=1 ORDER BY code"
    ).fetchall()]
    log("codes", total=len(codes))

    # ---- 端点健康探测: 当前 fund-flow 端点可能被东财限流, 等待恢复 ----
    probe_wait = 0
    while True:
        try:
            probe = ak.stock_individual_fund_flow(stock=codes[0], market=_market_of(codes[0]))
            if probe is not None:
                break
        except Exception:
            pass
        probe_wait = probe_wait * 2 if probe_wait else 30
        probe_wait = min(probe_wait, 600)
        log("endpoint_blocked", waiting_s=probe_wait)
        time.sleep(probe_wait)
    log("endpoint_ok", waited_s=probe_wait)

    wrote = 0
    failed = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        df = None
        for attempt in range(5):
            try:
                df = ak.stock_individual_fund_flow(stock=code, market=_market_of(code))
                if df is not None:
                    break
            except Exception:
                pass
            wait = min(30 * (attempt + 1), 300)
            log("retry", code=code, attempt=attempt + 1, wait_s=wait)
            time.sleep(wait)
        if df is None or df.empty:
            failed += 1
            continue
        drow = {}
        for _, r in df.iterrows():
            dt = str(r.get("日期", "")).strip()
            drow.setdefault(dt, r)
        recs = []
        for d in miss:
            if d not in drow:
                continue
            rr = drow[d]
            try:
                net = float(rr.get("主力净流入-净额") or 0)
            except (TypeError, ValueError):
                net = None
            if net is None:
                continue
            recs.append((code, d, net))
        for code_, d_, net in recs:
            conn.execute(
                "INSERT OR REPLACE INTO stock_moneyflow"
                " (code,date,main_net_inflow,retail_net_inflow,main_inflow,main_outflow)"
                " VALUES (?,?,?,0,0,0)",
                (code_, d_, net),
            )
            wrote += 1
        if i % 100 == 0:
            conn.commit()
            log("progress", codes_done=i, codes_total=len(codes),
                wrote=wrote, elapsed_s=round(time.time() - t0, 1))
        time.sleep(0.3)

    conn.commit()
    log("done", wrote=wrote, failed_codes=failed,
        elapsed_s=round(time.time() - t0, 1))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())