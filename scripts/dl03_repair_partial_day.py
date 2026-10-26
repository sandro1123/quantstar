"""DL-03 修复: 删除残缺日 2026-08-11 → tushare 批量重拉 → 校验≥4000行 → 单事务提交。

用户已预授权写库 (审计卡范围, 备份已完成: data/backups/klines_partial_days_20260818.{csv,db})。
任何一步失败 → rollback, 残缺日行保持原样。
"""
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/qs/.env"))

sys.path.insert(0, os.path.expanduser("~/qs/src"))

DB = os.path.expanduser("~/qs/data/qs.db")
PARTIAL_DATE = "2026-08-11"
MIN_ROWS = 4000

# 1. 重拉 (事务外, 只读外部源)
from data_layer.sources.tushare_source import TushareSourceAdapter

src = TushareSourceAdapter()
if not src.is_available():
    print("ABORT: tushare not available (no token?)")
    sys.exit(1)

records = src.get_daily_market(PARTIAL_DATE)
print(f"fetched from tushare: {len(records)} rows for {PARTIAL_DATE}")
if len(records) < MIN_ROWS:
    print(f"ABORT: fetched {len(records)} < {MIN_ROWS}, not refilling")
    sys.exit(1)

# 抽查 3 只与备份的 85 行对比 (重叠码的 close 应一致, 允许复权微差)
bak = sqlite3.connect(os.path.expanduser("~/qs/data/backups/klines_partial_days_20260818.db"))
old = {r[0]: r[1] for r in bak.execute("SELECT code, close FROM daily_klines_backup")}
bak.close()
fetched = {r["code"]: r["close"] for r in records}
overlap = set(old) & set(fetched)
print(f"overlap codes with backup: {len(overlap)}")
mismatch = [(c, old[c], fetched[c]) for c in list(overlap)[:20]
            if old[c] and fetched[c] and abs(old[c] - fetched[c]) / max(old[c], 0.01) > 0.03]
if mismatch:
    print(f"WARNING: close mismatch >3% on {len(mismatch)} codes: {mismatch[:5]}")
else:
    print("spot check OK: overlapping codes close within 3%")

# 2. 单事务: 删除 → 写入 → 校验 → 提交
conn = sqlite3.connect(DB, timeout=30)
conn.execute("PRAGMA busy_timeout=10000")
try:
    conn.execute("BEGIN IMMEDIATE")
    deleted = conn.execute("DELETE FROM daily_klines WHERE date = ?", (PARTIAL_DATE,)).rowcount
    print(f"deleted old rows: {deleted}")
    conn.executemany(
        "INSERT OR REPLACE INTO daily_klines "
        "(code, date, open, high, low, close, volume, amount, pre_close, change_pct, turnover_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["code"], r["date"], r.get("open", 0), r.get("high", 0), r.get("low", 0),
          r.get("close", 0), r.get("volume", 0), r.get("amount", 0),
          r.get("pre_close", 0), r.get("change_pct", 0), r.get("turnover_rate", 0))
         for r in records],
    )
    n = conn.execute("SELECT COUNT(*) FROM daily_klines WHERE date = ?", (PARTIAL_DATE,)).fetchone()[0]
    print(f"rows after refill: {n}")
    if n < MIN_ROWS:
        conn.execute("ROLLBACK")
        print(f"ABORT+ROLLBACK: {n} < {MIN_ROWS}")
        sys.exit(1)
    conn.execute("COMMIT")
    print("COMMIT_OK")
except Exception as e:
    conn.execute("ROLLBACK")
    print(f"EXCEPTION+ROLLBACK: {e}")
    sys.exit(1)
finally:
    conn.close()

# 3. 终验
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
final = conn.execute("SELECT COUNT(*) FROM daily_klines WHERE date = ?", (PARTIAL_DATE,)).fetchone()[0]
remaining = conn.execute(
    "SELECT date, COUNT(*) FROM daily_klines WHERE date>='2026-01-01' GROUP BY date HAVING COUNT(*)<4000"
).fetchall()
conn.close()
print(f"FINAL: {PARTIAL_DATE} rows = {final}; remaining partial days since 2026-01-01: {remaining}")
print("REPAIR_OK" if final >= MIN_ROWS and not remaining else "REPAIR_INCOMPLETE")
