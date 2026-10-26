#!/usr/bin/env python3
"""Read-only valuation/kline alignment audit for QuantStar P2-03.

Streams stock_valuation rows from valuation.db, cross-references against
daily_klines in qs.db, and classifies each gap into a primary category.
All temporary data in Python memory — no ATTACH DATABASE, no CREATE TEMP TABLE.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path: Path) -> dict:
    st = path.stat()
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": st.st_size,
        "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


def open_readonly_ro(path: Path | str) -> sqlite3.Connection:
    path = Path(path).resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=300)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")
    actual = conn.execute("PRAGMA query_only").fetchone()[0]
    if actual != 1:
        raise RuntimeError(f"query_only pragma returned {actual}, expected 1")
    conn.row_factory = sqlite3.Row
    return conn


def rss_bytes() -> int:
    try:
        return int(Path(f"/proc/{os.getpid()}/status").read_text().split("VmRSS:")[1].split()[0]) * 1024
    except Exception:
        return 0


def rss_human() -> str:
    b = rss_bytes()
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def is_weekend(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

PRIORITY_CATEGORIES = [
    "CODE_ABSENT_IN_KLINE",
    "BEFORE_FIRST_KLINE",
    "AFTER_LAST_KLINE",
    "DATE_NOT_IN_MARKET_CALENDAR",
    "WITHIN_RANGE_NO_KLINE",
    "CODE_OR_MARKET_ANOMALY",
    "OTHER_UNCLASSIFIED",
]


def classify_gap(
    code: str,
    date: str,
    kline_codes: set[str],
    market_dates: set[str],
    code_bounds: dict[str, tuple[str, str, int]],
) -> tuple[str, str]:
    """Return (primary_category, suspected_reason).

    Priority order is hard-coded — the first matching branch wins.
    suspected_reason is informational only, never used for counting.
    """
    # 1) code never appears in daily_klines at all
    if code not in kline_codes:
        return ("CODE_ABSENT_IN_KLINE", f"code {code} not found in daily_klines")

    bounds = code_bounds[code]
    first_kline, last_kline, kline_count = bounds

    # 2) date before first kline
    if date < first_kline:
        return ("BEFORE_FIRST_KLINE", f"date {date} before first kline {first_kline}")

    # 3) date after last kline
    if date > last_kline:
        return ("AFTER_LAST_KLINE", f"date {date} after last kline {last_kline}")

    # 4) date not in any market trading day
    if date not in market_dates:
        return ("DATE_NOT_IN_MARKET_CALENDAR", f"date {date} not in market calendar")

    # 5) code+date within range but no kline that day
    return ("WITHIN_RANGE_NO_KLINE", f"no kline for {code} on {date}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    tracemalloc.start()

    parser = argparse.ArgumentParser(description="Read-only valuation/kline alignment audit")
    parser.add_argument("--valuation-db", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=50000, help="rows per fetch batch")
    args = parser.parse_args()

    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_size = args.batch_size

    # ---- logging -----------------------------------------------------------
    run_log_path = output_dir / "run.log"
    logger = logging.getLogger("valuation_alignment")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(str(run_log_path), mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # also tee stdout/stderr to run.log via a separate stream
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr

    class TeeIO(io.TextIOBase):
        def __init__(self, *streams: IO[str]):
            self._streams = streams

        def write(self, s: str) -> int:
            for st in self._streams:
                st.write(s)
            return len(s)

        def flush(self) -> None:
            for st in self._streams:
                st.flush()

    log_file_handle = open(run_log_path, "a", encoding="utf-8")  # noqa: SIM115
    sys.stdout = TeeIO(orig_stdout, log_file_handle)
    sys.stderr = TeeIO(orig_stderr, log_file_handle)

    # ---- module-origin.txt -------------------------------------------------
    module_path = Path(__file__).resolve()
    origin_lines = [
        f"python: {sys.executable}",
        f"cwd: {Path.cwd()}",
        f"PYTHONPATH: {os.environ.get('PYTHONPATH', '')}",
        f"module __file__: {module_path}",
    ]
    (output_dir / "module-origin.txt").write_text("\n".join(origin_lines) + "\n", encoding="utf-8")

    # ---- fingerprints before -----------------------------------------------
    fp_before = {
        "valuation_db": fingerprint(args.valuation_db),
        "market_db": fingerprint(args.market_db),
    }
    (output_dir / "input-fingerprints-before.txt").write_text(
        json.dumps(fp_before, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    start_ts = time.time()
    logger.info("=== valuation alignment audit start ===")
    logger.info("valuation_db: %s", args.valuation_db.resolve())
    logger.info("market_db: %s", args.market_db.resolve())
    logger.info("output_dir: %s", output_dir)
    logger.info("batch_size: %d", batch_size)

    # ---- open databases (read-only) ----------------------------------------
    try:
        vconn = open_readonly_ro(args.valuation_db)
        mconn = open_readonly_ro(args.market_db)
    except Exception as exc:
        logger.error("Failed to open databases: %s", exc)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_file_handle.close()
        return 1

    # ---- load kline reference data into memory -----------------------------
    logger.info("Loading kline code set ...")
    kline_codes: set[str] = set()
    cursor = mconn.execute("SELECT DISTINCT code FROM daily_klines")
    for row in cursor:
        kline_codes.add(row["code"])
    logger.info("  %d distinct codes in daily_klines", len(kline_codes))

    logger.info("Loading market dates (distinct trading days) ...")
    market_dates: set[str] = set()
    cursor = mconn.execute("SELECT DISTINCT date FROM daily_klines ORDER BY date")
    for row in cursor:
        market_dates.add(row["date"])
    logger.info("  %d distinct dates in daily_klines", len(market_dates))
    market_dates_sorted = sorted(market_dates)
    min_market_date = market_dates_sorted[0] if market_dates_sorted else ""
    max_market_date = market_dates_sorted[-1] if market_dates_sorted else ""

    logger.info("Loading code bounds (first/last kline per code) ...")
    code_bounds: dict[str, tuple[str, str, int]] = {}
    cursor = mconn.execute(
        "SELECT code, MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS kline_rows "
        "FROM daily_klines GROUP BY code"
    )
    for row in cursor:
        code_bounds[row["code"]] = (row["first_date"], row["last_date"], row["kline_rows"])
    logger.info("  %d code bounds loaded", len(code_bounds))

    # ---- stream valuation rows, classify -----------------------------------
    total_valuation: int = vconn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    logger.info("Total valuation rows: %d", total_valuation)

    gaps: list[dict] = []
    by_code: dict[str, Counter] = defaultdict(Counter)
    by_date: dict[str, Counter] = defaultdict(Counter)
    by_date_codes: dict[str, set[str]] = defaultdict(set)
    code_meta: dict[str, dict] = {}

    batched = 0
    next_heartbeat = 100000
    cumul: Counter[str] = Counter()

    cursor = vconn.execute(
        "SELECT code, date FROM stock_valuation ORDER BY code, date"
    )
    kline_cursor = mconn.execute(
        "SELECT code, date FROM daily_klines ORDER BY code, date"
    )
    current_kline = kline_cursor.fetchone()
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            for row in rows:
                code: str = row["code"]
                date: str = row["date"]
                while current_kline is not None and (current_kline["code"], current_kline["date"]) < (code, date):
                    current_kline = kline_cursor.fetchone()
                if current_kline is not None and current_kline["code"] == code and current_kline["date"] == date:
                    continue

                cat, reason = classify_gap(code, date, kline_codes, market_dates, code_bounds)
                gaps.append({"code": code, "date": date, "primary_category": cat, "suspected_reason": reason})

                cumul[cat] += 1
                by_code[code][cat] += 1
                by_date[date][cat] += 1
                by_date_codes[date].add(code)

                if code not in code_meta:
                    code_meta[code] = {
                        "first_mismatch": date,
                        "last_mismatch": date,
                    }
                    if code in code_bounds:
                        code_meta[code]["first_kline"] = code_bounds[code][0]
                        code_meta[code]["last_kline"] = code_bounds[code][1]
                        code_meta[code]["kline_rows"] = code_bounds[code][2]
                    else:
                        code_meta[code]["first_kline"] = None
                        code_meta[code]["last_kline"] = None
                        code_meta[code]["kline_rows"] = 0
                else:
                    meta = code_meta[code]
                    if date < meta["first_mismatch"]:
                        meta["first_mismatch"] = date
                    if date > meta["last_mismatch"]:
                        meta["last_mismatch"] = date

            batched += len(rows)

            if batched >= next_heartbeat:
                logger.info(
                    "  heartbeat: %d / %d rows processed (%d gaps) | RSS %s | cats: %s",
                    batched, total_valuation, len(gaps), rss_human(),
                    dict(cumul.most_common()),
                )
                next_heartbeat = batched + 100000 - (batched % 100000) if batched % 100000 else batched + 100000
    except Exception as exc:
        logger.exception("Mid-stream failure at %d rows: %s", batched, exc)
        try:
            mconn.close()
            vconn.close()
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            log_file_handle.close()
        return 1

    mconn.close()
    vconn.close()

    elapsed = time.time() - start_ts
    logger.info("Streaming complete: %d rows in %.1fs | %d gaps | RSS %s",
                batched, elapsed, len(gaps), rss_human())

    # ---- aggregate results --------------------------------------------------
    total_cat_counts: Counter[str] = Counter()
    for g in gaps:
        total_cat_counts[g["primary_category"]] += 1

    distinct_codes_mismatch = len(by_code)
    distinct_dates_mismatch = len(by_date)

    # ---- 2026-07-08 / 2026-07-20 analysis ----------------------------------
    anomaly_dates: dict[str, dict] = {}
    for target_date in ("2026-07-08", "2026-07-20"):
        matching = [g for g in gaps if g["date"] == target_date]
        cat_counts = Counter(g["primary_category"] for g in matching)
        codes = sorted(set(g["code"] for g in matching))
        weekend = is_weekend(target_date)
        in_market = target_date in market_dates
        anomaly_dates[target_date] = {
            "total_gaps": len(matching),
            "distinct_codes": len(codes),
            "category_counts": dict(cat_counts),
            "is_weekend": weekend,
            "in_market_calendar": in_market,
            "sample_codes": codes[:20],
            "sample_codes_total": len(codes),
        }

    # ---- fingerprints after ------------------------------------------------
    fp_after = {
        "valuation_db": fingerprint(args.valuation_db),
        "market_db": fingerprint(args.market_db),
    }
    unchanged = (
        fp_before["valuation_db"]["sha256"] == fp_after["valuation_db"]["sha256"]
        and fp_before["market_db"]["sha256"] == fp_after["market_db"]["sha256"]
    )

    # ---- alignment-summary.json --------------------------------------------
    exact_matches = total_valuation - len(gaps)
    summary = {
        "audited_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "valuation_db": str(args.valuation_db.resolve()),
        "market_db": str(args.market_db.resolve()),
        "input_fingerprints_before": fp_before,
        "input_fingerprints_after": fp_after,
        "inputs_unchanged": unchanged,
        "total_valuation_rows": total_valuation,
        "exact_code_date_matches": exact_matches,
        "gap_rows": len(gaps),
        "exact_match_ratio": round(exact_matches / total_valuation, 6) if total_valuation else None,
        "distinct_gap_codes": distinct_codes_mismatch,
        "distinct_gap_dates": distinct_dates_mismatch,
        "primary_category_counts": {k: total_cat_counts.get(k, 0) for k in PRIORITY_CATEGORIES},
        "market_calendar": {
            "distinct_dates": len(market_dates),
            "min_date": min_market_date,
            "max_date": max_market_date,
            "distinct_kline_codes": len(kline_codes),
        },
        "anomaly_date_analysis": anomaly_dates,
        "elapsed_seconds": round(elapsed, 2),
        "peak_rss_bytes": tracemalloc.get_traced_memory()[1],
        "peak_rss_human": rss_human(),
        "batch_size": batch_size,
    }
    summary_path = output_dir / "alignment-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote %s", summary_path)

    # ---- alignment-by-code.csv ---------------------------------------------
    by_code_path = output_dir / "alignment-by-code.csv"
    with by_code_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["code", "primary_category", "mismatch_count",
                         "first_mismatch", "last_mismatch",
                         "first_kline", "last_kline", "kline_rows"])
        for code in sorted(by_code):
            cc = by_code[code]
            dominant = cc.most_common(1)[0][0] if cc else "OTHER_UNCLASSIFIED"
            total = sum(cc.values())
            meta = code_meta.get(code, {})
            writer.writerow([
                code, dominant, total,
                meta.get("first_mismatch", ""), meta.get("last_mismatch", ""),
                meta.get("first_kline", ""), meta.get("last_kline", ""),
                meta.get("kline_rows", 0),
            ])
    logger.info("Wrote %s (%d codes)", by_code_path, len(by_code))

    # ---- alignment-by-date.csv ---------------------------------------------
    by_date_path = output_dir / "alignment-by-date.csv"
    with by_date_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "mismatch_count", "distinct_codes", "primary_category"])
        for date in sorted(by_date):
            dc = by_date[date]
            dominant = dc.most_common(1)[0][0] if dc else "OTHER_UNCLASSIFIED"
            distinct_codes = len(by_date_codes.get(date, set()))
            total = sum(dc.values())
            writer.writerow([date, total, distinct_codes, dominant])
    logger.info("Wrote %s (%d dates)", by_date_path, len(by_date))

    # ---- alignment-gaps.csv.gz ---------------------------------------------
    gaps_gz_path = output_dir / "alignment-gaps.csv.gz"
    with gzip.open(gaps_gz_path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["code", "date", "primary_category", "suspected_reason"])
        for g in sorted(gaps, key=lambda x: (x["code"], x["date"])):
            writer.writerow([g["code"], g["date"], g["primary_category"], g["suspected_reason"]])
    logger.info("Wrote %s (%d gaps)", gaps_gz_path, len(gaps))

    # ---- anomaly-dates.json ------------------------------------------------
    anomaly_path = output_dir / "anomaly-dates.json"
    anomaly_path.write_text(
        json.dumps(anomaly_dates, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote %s", anomaly_path)

    # ---- fingerprints after (file) -----------------------------------------
    (output_dir / "input-fingerprints-after.txt").write_text(
        json.dumps(fp_after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Databases unchanged: %s", unchanged)

    # ---- final summary -----------------------------------------------------
    total_elapsed = time.time() - start_ts
    logger.info("=== audit complete in %.1fs ===", total_elapsed)
    logger.info("Gaps by category:")
    for cat in PRIORITY_CATEGORIES:
        logger.info("  %-30s %d", cat, total_cat_counts.get(cat, 0))
    logger.info("Output directory: %s", output_dir)

    # restore stdout/stderr
    sys.stdout = orig_stdout
    sys.stderr = orig_stderr
    log_file_handle.close()

    print(f"\nAudit complete. Output: {output_dir}")
    print(f"  alignment-summary.json")
    print(f"  alignment-by-code.csv")
    print(f"  alignment-by-date.csv")
    print(f"  alignment-gaps.csv.gz")
    print(f"  anomaly-dates.json")
    print(f"  run.log")
    print(f"  module-origin.txt")
    print(f"  input-fingerprints-before.txt")
    print(f"  input-fingerprints-after.txt")
    print(f"Total gaps: {len(gaps)} / {total_valuation} rows")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())