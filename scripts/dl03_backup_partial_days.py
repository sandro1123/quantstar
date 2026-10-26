"""DL-03 备份: 导出残缺日 2026-08-11 的 daily_klines 行到 CSV + 独立 db。只读+写备份, 不碰生产表。"""
import csv
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/qs/.env"))

DB = os.path.expanduser("~/qs/data/qs.db")
BACKUP_DIR = os.path.expanduser("~/qs/data/backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
CSV_PATH = os.path.join(BACKUP_DIR, "klines_partial_days_20260818.csv")
DBBAK_PATH = os.path.join(BACKUP_DIR, "klines_partial_days_20260818.db")

PARTIAL_DATE = "2026-08-11"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT code, date, open, high, low, close, volume, amount, pre_close, change_pct, turnover_rate, adjust_type "
    "FROM daily_klines WHERE date = ? ORDER BY code",
    (PARTIAL_DATE,),
).fetchall()
conn.close()

print(f"rows to backup for {PARTIAL_DATE}: {len(rows)}")
if not rows:
    print("NO ROWS — abort, nothing to backup")
    sys.exit(1)

# CSV backup
cols = rows[0].keys()
with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
print(f"CSV written: {CSV_PATH} ({os.path.getsize(CSV_PATH)} bytes)")

# Independent db backup
bak = sqlite3.connect(DBBAK_PATH)
bak.execute("""
    CREATE TABLE IF NOT EXISTS daily_klines_backup (
        code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, pre_close REAL, change_pct REAL,
        turnover_rate REAL, adjust_type TEXT
    )
""")
bak.executemany(
    "INSERT INTO daily_klines_backup VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    [tuple(r[c] for c in cols) for r in rows],
)
bak.commit()
n = bak.execute("SELECT COUNT(*) FROM daily_klines_backup").fetchone()[0]
bak.close()
print(f"DB backup written: {DBBAK_PATH} ({n} rows)")
print("BACKUP_OK")
