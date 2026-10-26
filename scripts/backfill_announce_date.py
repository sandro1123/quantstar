"""DL-47 + DL-48: stock_financials 补 announce_date + caliber 列

公告日 (announce_date):
  fuyao 三财报 API 返回 report_date_ms (公告日毫秒时间戳)。
  本脚本从 fuyao 拉取每只股票每个报告期的公告日，回填到 stock_financials。

口径 (caliber):
  现有数据为累计报告值 (cumulative) — financial_caliber.py 的原始输入。
  Q1 累计 = 单季值; Q2/Q3/Q4 累计需差分得单季值。标 'cumulative'。

三步曲用法:
  # 1) 只读分类 (dry-run, 不改库): 统计需回补行数 + fuyao 覆盖率
  python scripts/backfill_announce_date.py --dry-run --db data/qs.db

  # 2) 隔离库验证 (先 cp 出副本, 对副本 --apply):
  cp data/qs.db /tmp/qs_iso.db
  python scripts/backfill_announce_date.py --apply --db /tmp/qs_iso.db

  # 3) 生产 --apply 需统筹 GO, 不要自行执行。
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# D1-b (修法①同款): fuyao period_end_ms/report_date_ms 是 BJT 毫秒戳。
# UTC 渲染把 06-30 16:00 BJT 落成 06-29 → period/announce 双 off-by-one
# (123 病行族根因; 修法① 0fa39aa 修了 fuyao.py 三处, 本脚本漏修)。
BJT = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 显式加载 .env (无参 load_dotenv 在某些 cwd 下找不到文件)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "qs.db")

ALTER_SQL = [
    "ALTER TABLE stock_financials ADD COLUMN announce_date TEXT DEFAULT ''",
    "ALTER TABLE stock_financials ADD COLUMN caliber TEXT DEFAULT 'cumulative'",
]


def has_columns(conn: sqlite3.Connection) -> dict[str, bool]:
    cur = conn.execute("PRAGMA table_info(stock_financials)")
    existing = {r[1] for r in cur.fetchall()}
    return {"announce_date": "announce_date" in existing, "caliber": "caliber" in existing}


def ensure_columns(conn: sqlite3.Connection) -> list[str]:
    existing = has_columns(conn)
    added = []
    for sql in ALTER_SQL:
        col = sql.split("ADD COLUMN ")[1].split(" ")[0]
        if not existing[col]:
            conn.execute(sql)
            added.append(col)
    if added:
        conn.commit()
    return added


def ms_to_date(ms) -> str:
    if not ms:
        return ""
    try:
        # D1-b: BJT 读法 (修法①同款) — 1786636800000 = 08-14 00:00 BJT
        # 旧 UTC 读法落 08-13 → announce/period 双 off-by-one 病行族。
        return datetime.fromtimestamp(float(ms) / 1000, tz=BJT).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _thscode(code: str) -> str | None:
    """映射到 fuyao thscode。北交所 (4xx/8xx/92x) fuyao 无此市场 → 返回 None 跳过。

    若把 BJ 码误映成 .SZ, fuyao 返回空/报错, 连续失败会触发 FuyaoClient 熔断
    (3 连败 → 300s 锁), 静默丢弃后续全部抓取 — 这是首跑覆盖率仅 7.1% 的根因。
    """
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return None  # 北交所 / 其他, fuyao 不覆盖


def fetch_announce_dates(client, codes: list[str], limit: int = 12) -> dict[tuple[str, str], str]:
    """从 fuyao 拉取公告日。返回 {(code, period_end_date): announce_date}"""
    result = {}
    total = len(codes)
    skipped_bj = 0
    errors = 0
    breaker_waits = 0
    for i, code in enumerate(codes):
        if i % 200 == 0:
            print(f"  [{i}/{total}] {code}... (matched={len(result)} err={errors} bj_skip={skipped_bj})", flush=True)
        ths = _thscode(code)
        if ths is None:
            skipped_bj += 1
            continue
        # 熔断保护: 若熔断器打开则等待其复位, 避免在锁定期空转
        while not client.available():
            breaker_waits += 1
            if breaker_waits <= 3:
                print(f"  [circuit open] waiting 60s for breaker reset...", flush=True)
            time.sleep(60)
        try:
            rows = client.get_income_statements(ths, period="quarterly", limit=limit)
            for r in rows:
                pe = ms_to_date(r.get("period_end_ms"))
                rd = ms_to_date(r.get("report_date_ms"))
                if pe and rd:
                    result[(code, pe)] = rd
        except Exception:
            errors += 1
            continue
        # rate limit: ~4 req/s (留余量避免触发上游限流)
        if i % 4 == 3:
            time.sleep(1.0)
    print(f"  fetch done: matched_pairs={len(result)} errors={errors} bj_skipped={skipped_bj} breaker_waits={breaker_waits}", flush=True)
    return result


def match_announce_dates(need, announce_map):
    """匹配 (code, date) -> announce_date, 返回 (updates, unmatched)。

    精确键命中优先; 未命中时回退 date-1day 键 — fuyao 的 period_end 恒为
    季末日前一天 (03-30/06-29/09-29/12-30), 而库内旧尾行用标准季末日
    (03-31/06-30/09-30/12-31), 两者 off-by-one。回退仅对精确键未命中者生效;
    近期批次 (库内 date 已=fuyao 口径) 精确键即命中, 不受影响。

    updates=[(announce_date, code, date)] 与 UPDATE 参数序一致;
    unmatched=[(code, date)] 供残余清单落档。
    """
    updates = []
    unmatched = []
    for code, date in sorted(need):
        ann = announce_map.get((code, date), "")
        if not ann:
            prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            ann = announce_map.get((code, prev), "")
        if ann:
            updates.append((ann, code, date))
        else:
            unmatched.append((code, date))
    return updates, unmatched

def main():
    parser = argparse.ArgumentParser(description="DL-47/48: backfill announce_date + caliber")
    parser.add_argument("--apply", action="store_true", help="实际写库 (默认只读 dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="只读分类, 不改库 (默认行为)")
    parser.add_argument("--backup", action="store_true", help="--apply 前先备份")
    parser.add_argument("--db", default=DEFAULT_DB, help="目标库路径 (隔离验证用)")
    parser.add_argument("--limit", type=int, default=12, help="每股拉取报告期数")
    parser.add_argument("--codes", nargs="*", help="指定股票代码 (默认全市场)")
    parser.add_argument("--residual-out", default="", help="把残余未匹配 (code,date) 清单写入该文件 (残余落档用)")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    apply = args.apply
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN (read-only)'}")

    if apply:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Step 1: columns
    cols = has_columns(conn)
    print(f"Columns present: {cols}")

    # Step 2: codes
    if args.codes:
        codes = args.codes
    else:
        rows = conn.execute("SELECT code FROM stock_info WHERE is_active=1").fetchall()
        codes = [r["code"] for r in rows]
    print(f"Codes: {len(codes)}")

    # Step 3: rows needing announce_date
    if cols["announce_date"]:
        existing = conn.execute(
            "SELECT code, date FROM stock_financials "
            "WHERE announce_date IS NULL OR announce_date = ''"
        ).fetchall()
    else:
        existing = conn.execute("SELECT code, date FROM stock_financials").fetchall()
    need = {(r["code"], r["date"]) for r in existing}
    if args.codes:
        codeset = set(args.codes)
        need = {(c, d) for c, d in need if c in codeset}
    total_rows = conn.execute("SELECT COUNT(*) c FROM stock_financials").fetchone()["c"]
    print(f"Total rows: {total_rows}; needing announce_date (in scope): {len(need)}")

    # date distribution (诊断: 非季末日无法匹配 fuyao period_end)
    dist = conn.execute(
        "SELECT date, COUNT(*) c FROM stock_financials GROUP BY date ORDER BY c DESC LIMIT 8"
    ).fetchall()
    print("Date distribution (top):")
    for r in dist:
        print(f"  {r['date']}: {r['c']}")

    if not need:
        print("Nothing to do.")
        conn.close()
        return

    # Step 4: fetch from fuyao
    from data_layer.sources.fuyao_client import FuyaoClient
    client = FuyaoClient()
    if not client.api_key:
        print("ERROR: FUYAO_API_KEY not set")
        sys.exit(1)
    if not client.available():
        print("ERROR: fuyao not available")
        sys.exit(1)

    codes_needed = sorted({c for c, d in need})
    if args.codes:
        codes_needed = [c for c in codes_needed if c in set(args.codes)]
    print(f"Fetching announce dates for {len(codes_needed)} codes (limit={args.limit})...")
    announce_map = fetch_announce_dates(client, codes_needed, limit=args.limit)
    print(f"Fetched {len(announce_map)} (code, period) -> announce_date pairs")

    # Step 5: match (纯函数, 含 off-by-one 回退; 见 match_announce_dates)
    updates, unmatched_pairs = match_announce_dates(need, announce_map)
    matched, unmatched = len(updates), len(unmatched_pairs)

    print(f"\nResults: matched={matched}, unmatched={unmatched}")
    if need:
        print(f"Coverage: {matched}/{len(need)} = {matched/len(need)*100:.1f}%")

    # 残余清单落档: 未匹配 (code,date) = 源端确无/超出 fuyao 12 季窗口的诚实记录
    if args.residual_out and unmatched_pairs:
        with open(args.residual_out, "w", encoding="utf-8") as fh:
            fh.write("code,date\n")
            for code, date in unmatched_pairs:
                fh.write(f"{code},{date}\n")
        print(f"Residual list ({len(unmatched_pairs)} rows) -> {args.residual_out}")

    if apply:
        if args.backup:
            import shutil
            backup_path = db_path.replace(".db", "_pre_dl47.db")
            print(f"Backing up to {backup_path}...")
            shutil.copy2(db_path, backup_path)
        added = ensure_columns(conn)
        if added:
            print(f"Added columns: {added}")
        print(f"Applying {len(updates)} announce_date updates...")
        conn.executemany(
            "UPDATE stock_financials SET announce_date = ? WHERE code = ? AND date = ?",
            updates,
        )
        conn.execute(
            "UPDATE stock_financials SET caliber = 'cumulative' "
            "WHERE caliber IS NULL OR caliber = ''"
        )
        conn.commit()
        # verify
        filled = conn.execute(
            "SELECT COUNT(*) c FROM stock_financials WHERE length(announce_date) > 0"
        ).fetchone()["c"]
        print(f"Done. Rows with announce_date now: {filled}/{total_rows}")
    else:
        print(f"\nDRY-RUN: would update {len(updates)} rows (no writes made).")
        if updates:
            print(f"Sample: {updates[:5]}")

    conn.close()


if __name__ == "__main__":
    main()
