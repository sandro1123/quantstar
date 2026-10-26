#!/usr/bin/env python3
"""Classify 75 BaoStock backfill failures into root-cause categories (P2-04).

Read-only audit: opens market DB and valuation DB via mode=ro + PRAGMA query_only=ON.
Streams the 75 failed codes from backfill.jsonl, cross-references against
stock_info / daily_klines / valuation_provenance, and assigns one primary_category
per code.

Only TRANSIENT_NETWORK suggests retry; all other categories are permanent.
"""

from __future__ import annotations

import argparse
import csv
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
from typing import Any


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------

CATEGORIES = [
    "DELISTED_OR_LEGACY",
    "MERGER_OR_REPLACED",
    "CODE_MIGRATION",
    "MARKET_LABEL_ERROR",
    "TEST_OR_POLLUTION",
    "SOURCE_HAS_NO_HISTORY",
    "QUERY_RANGE_OR_FORMAT",
    "TRANSIENT_NETWORK",
    "UNKNOWN_MANUAL_REVIEW",
]

RETRY_ALLOWED = {"TRANSIENT_NETWORK"}

# Known merger/replacement — A-share stocks that were absorbed or privatised.
# Source: public merger records / historic delisting events.
_MERGED_CODES: set[str] = {
    "000406",  # 石油大明 → 中国石油 absorption
    "000508",  # 琼民源A → 中关村 code swap
    "000515",  # 攀渝钛业 → 攀钢钒钛 merger
    "000542",  # TCL通讯 → TCL集团 absorption
    "000549",  # S湘火炬 → 潍柴动力 absorption
    "000569",  # 长城股份 → 攀钢钒钛 merger
    "000618",  # 吉林化工 → 中国石油 absorption
    "000763",  # 锦州石化 → 中国石油 absorption
    "000817",  # 辽河油田 → 中国石油 absorption
    "000866",  # 扬子石化 → 中国石油 absorption
    "000956",  # 中原油气 → 中国石油 absorption
    "600001",  # 邯郸钢铁 → 唐钢股份 merger
    "600002",  # 齐鲁石化 → 中国石油化工 absorption
    "600205",  # S山东铝 → 中国铝业 absorption
    "600296",  # S兰铝   → 中国铝业 absorption
    "600357",  # 承德钒钛 → 河北钢铁 merger
    "600472",  # 包头铝业 → 中国铝业 absorption
    "600627",  # 上电股份 → 上海电气 absorption
    "600632",  # 华联商厦 → 百联股份 merger
    "600786",  # 东方锅炉 → 东方电气 absorption
    "600840",  # 新湖创业 → 新湖中宝 absorption
}

# Known code migrations — old code reassigned or migrated to a new board.
_MIGRATED_CODES: set[str] = {
    "000015",  # PT中浩A → 400011 老三板
    "000556",  # PT南洋  → 400023 老三板
    "000588",  # PT粤金曼 → 400012 老三板
    "000625",  # PT水仙  → 400008 老三板 (600625)
}

# Known test / pollution codes
_TEST_CODES: set[str] = {
    "001235",  # 无效里得 — literal "invalid" name
}

# IPO-failed codes — stock code was assigned but listing never completed
_IPO_FAILED_CODES: set[str] = {
    "002257",  # 立立电子 — IPO approved but cancelled
    "002525",  # 胜景山河 — IPO fraud, revoked
    "002720",  # 宏良股份 — IPO failed
    "600349",  # 富通昭和 — IPO cancelled
    "603302",  # 鑫广绿环 — IPO suspended
    "603361",  # 浙江国祥 — IPO suspended
    "688688",  # 蚂蚁集团 — IPO halted
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def load_drill_candidates(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    candidates: set[str] = set()

    # sample_findings contains per-category sample lists
    for cat_items in raw.get("sample_findings", {}).values():
        if isinstance(cat_items, list):
            for item in cat_items:
                if isinstance(item, (list, tuple)) and len(item) > 0:
                    candidates.add(str(item[0]))
                elif isinstance(item, str):
                    candidates.add(item)

    # candidate_unique is just the count; the actual 239 list is inferred
    # from findings_counts: all codes with placeholder_list_date_20260518
    # are drill candidates. We handle this via the placeholder date check
    # in classify_code() rather than maintaining a static list here.
    return candidates


def load_alignment_by_code(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("code", "")
            if code:
                result[code] = dict(row)
    return result


def name_has_st_flag(name: str) -> bool:
    upper = name.upper()
    return (
        upper.startswith("*ST")
        or upper.startswith("ST")
        or upper.startswith("PT")
        or upper.startswith("S*ST")
        or upper.startswith("S ST")
        or "SST" in upper
    )


def is_placeholder_list_date(list_date: str | None) -> bool:
    return list_date is not None and list_date == "20260518"


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def classify_code(
    code: str,
    info: dict[str, Any] | None,
    kline_info: dict[str, Any] | None,
    provenance_info: dict[str, Any] | None,
    in_drill: bool,
    alignment: dict[str, Any] | None,
    drill_counts: dict[str, int] | None = None,
) -> tuple[str, str]:
    """Return (primary_category, reasoning).

    Priority order is hard-coded — first matching branch wins.
    """

    name = (info or {}).get("name", "") or ""
    market = (info or {}).get("market", "") or ""
    list_date = (info or {}).get("list_date", "") or ""
    is_active = (info or {}).get("is_active", 1)
    kline_count = (kline_info or {}).get("count", 0)
    provenance_error = (provenance_info or {}).get("error_type", "") or ""

    # 1) TEST_OR_POLLUTION
    if code in _TEST_CODES:
        return ("TEST_OR_POLLUTION", "explicit test/pollution code in _TEST_CODES")

    if "无效" in name or "TEST" in name.upper():
        return ("TEST_OR_POLLUTION", f"name contains invalid/test indicator: {name}")

    # 2) IPO_FAILED — never actually traded
    if code in _IPO_FAILED_CODES:
        return ("SOURCE_HAS_NO_HISTORY", "IPO never completed; BaoStock has no history")

    # 3) MERGER_OR_REPLACED
    if code in _MERGED_CODES:
        return ("MERGER_OR_REPLACED", "known merger/absorption; code no longer trades independently")

    # 4) CODE_MIGRATION
    if code in _MIGRATED_CODES:
        return ("CODE_MIGRATION", "code migrated to another board (e.g. 老三板)")

    # 5) DELISTED_OR_LEGACY
    if name_has_st_flag(name):
        return ("DELISTED_OR_LEGACY", f"ST/PT flagged stock: {name}")

    if is_placeholder_list_date(list_date):
        return ("DELISTED_OR_LEGACY", "placeholder list_date 20260518 — likely legacy/delisted")

    if not is_active:
        return ("DELISTED_OR_LEGACY", "is_active=0 in stock_info")

    # 6) MARKET_LABEL_ERROR
    if market:
        if code.startswith("6") and market != "SH":
            return ("MARKET_LABEL_ERROR", f"code {code} starts with 6 but market={market}")
        if code.startswith(("0", "3")) and market != "SZ":
            return ("MARKET_LABEL_ERROR", f"code {code} starts with {code[0]} but market={market}")
        if code.startswith(("4", "8")) and market not in ("BJ", "SH", "SZ"):
            return ("MARKET_LABEL_ERROR", f"code {code} starts with {code[0]} but market={market}")

    # 7) SOURCE_HAS_NO_HISTORY
    if provenance_error == "BackfillRefusedError":
        return ("SOURCE_HAS_NO_HISTORY", "BaoStock returned empty result for this code")

    # 8) QUERY_RANGE_OR_FORMAT — check if code has klines indicating recent listing
    #    but BaoStock was queried with wrong range/format
    if kline_count > 0:
        return ("QUERY_RANGE_OR_FORMAT", f"has {kline_count} klines in qs.db but BaoStock returned empty")

    # 9) TRANSIENT_NETWORK — only if it looks like a transient issue
    if provenance_error:
        return ("TRANSIENT_NETWORK", f"provenance error '{provenance_error}' may be transient")

    # 10) fallback
    return ("UNKNOWN_MANUAL_REVIEW", "no matching classification rule")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    tracemalloc.start()

    parser = argparse.ArgumentParser(description="Classify empty BaoStock backfill results (P2-04)")
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--valuation-db", type=Path, required=True)
    parser.add_argument("--backfill-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--alignment-by-code", type=Path, default=None, help="optional P2-03 alignment-by-code.csv")
    parser.add_argument("--drill-json", type=Path, default=None, help="optional P2-02 pollution-drill drill.json")
    args = parser.parse_args()

    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- logging -----------------------------------------------------------
    run_log_path = output_dir / "run.log"
    logger = logging.getLogger("classify_empty")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(str(run_log_path), mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    log_file_handle = open(run_log_path, "a", encoding="utf-8")  # noqa: SIM115
    sys.stdout = _TeeIO(orig_stdout, log_file_handle)
    sys.stderr = _TeeIO(orig_stderr, log_file_handle)

    start_ts = time.time()
    logger.info("=== classify empty results start ===")
    logger.info("market_db: %s", args.market_db.resolve())
    logger.info("valuation_db: %s", args.valuation_db.resolve())
    logger.info("backfill_jsonl: %s", args.backfill_jsonl.resolve())
    logger.info("output_dir: %s", output_dir)

    # ---- load drill candidates (sample only; full list inferred) -----------
    drill_candidates: set[str] = set()
    drill_counts: dict[str, int] = {}
    if args.drill_json is not None:
        drill_path = Path(args.drill_json).resolve()
        if drill_path.exists():
            drill_candidates = load_drill_candidates(drill_path)
            drill_raw = json.loads(drill_path.read_text(encoding="utf-8"))
            drill_counts = drill_raw.get("findings_counts", {})
    logger.info("drill candidates loaded: %d (sample), counts=%s",
                len(drill_candidates), drill_counts)

    # ---- load alignment-by-code (optional) ----------------------------------
    alignment = load_alignment_by_code(args.alignment_by_code)
    logger.info("alignment-by-code records loaded: %d", len(alignment))

    # ---- open databases ----------------------------------------------------
    try:
        mconn = open_readonly_ro(args.market_db)
        vconn = open_readonly_ro(args.valuation_db)
    except Exception as exc:
        logger.error("Failed to open databases: %s", exc)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_file_handle.close()
        return 1

    # ---- load failed codes from backfill.jsonl -----------------------------
    failed_codes: list[dict[str, Any]] = []
    with args.backfill_jsonl.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("event") == "failed":
                failed_codes.append(rec)
    logger.info("Failed codes from backfill.jsonl: %d", len(failed_codes))

    # ---- batch load stock_info for all failed codes ------------------------
    codes_list = [r["code"] for r in failed_codes]
    placeholders = ",".join("?" for _ in codes_list)
    stock_info_map: dict[str, dict[str, Any]] = {}
    cursor = mconn.execute(
        f"SELECT code, name, industry, market, list_date, is_active FROM stock_info "
        f"WHERE code IN ({placeholders})",
        codes_list,
    )
    for row in cursor:
        stock_info_map[row["code"]] = dict(row)
    logger.info("stock_info records loaded: %d", len(stock_info_map))

    # ---- batch load kline summary ------------------------------------------
    kline_map: dict[str, dict[str, Any]] = {}
    cursor = mconn.execute(
        f"SELECT code, MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS count "
        f"FROM daily_klines WHERE code IN ({placeholders}) GROUP BY code",
        codes_list,
    )
    for row in cursor:
        kline_map[row["code"]] = dict(row)
    logger.info("kline summaries loaded: %d", len(kline_map))

    # ---- batch load provenance/backfill_state from valuation.db -------------
    provenance_map: dict[str, dict[str, Any]] = {}
    cursor = vconn.execute(
        f"SELECT code, status, error_type, error_message, rows_written, elapsed_s "
        f"FROM valuation_backfill_state WHERE code IN ({placeholders})",
        codes_list,
    )
    for row in cursor:
        provenance_map[row["code"]] = dict(row)
    logger.info("backfill_state records loaded: %d", len(provenance_map))

    mconn.close()
    vconn.close()

    # ---- classify each code ------------------------------------------------
    results: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()

    for rec in failed_codes:
        code = rec["code"]
        info = stock_info_map.get(code)
        kline_info = kline_map.get(code)
        prov_info = provenance_map.get(code)
        in_drill = code in drill_candidates or is_placeholder_list_date(
            (info or {}).get("list_date")
        )
        align = alignment.get(code)

        cat, reason = classify_code(code, info, kline_info, prov_info, in_drill, align, drill_counts)
        by_category[cat] += 1

        result: dict[str, Any] = {
            "code": code,
            "primary_category": cat,
            "suspected_reason": reason,
            "in_pollution_drill": in_drill,
            "in_p2p3_alignment": align is not None,
            "suggest_retry": cat in RETRY_ALLOWED,
            "backfill_error_type": prov_info.get("error_type", "") if prov_info else "",
            "backfill_error_message": prov_info.get("error_message", "") if prov_info else "",
            "backfill_elapsed_s": prov_info.get("elapsed_s", 0) if prov_info else 0,
            "stock_name": (info or {}).get("name", ""),
            "stock_market": (info or {}).get("market", ""),
            "stock_list_date": (info or {}).get("list_date", ""),
            "stock_is_active": (info or {}).get("is_active", -1),
            "kline_count": (kline_info or {}).get("count", 0),
            "kline_first_date": (kline_info or {}).get("first_date", ""),
            "kline_last_date": (kline_info or {}).get("last_date", ""),
        }
        results.append(result)

        logger.info(
            "  %s → %s | %s | klines=%d drill=%s align=%s",
            code, cat, (info or {}).get("name", "?"),
            (kline_info or {}).get("count", 0),
            in_drill, align is not None,
        )

    elapsed = time.time() - start_ts
    logger.info("Classification complete: %d codes in %.1fs | RSS %s",
                len(results), elapsed, rss_human())

    # ---- output: per-code JSON ---------------------------------------------
    per_code_path = output_dir / "classification-per-code.json"
    per_code_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote %s (%d codes)", per_code_path, len(results))

    # ---- output: CSV -------------------------------------------------------
    csv_path = output_dir / "classification-by-code.csv"
    fieldnames = [
        "code", "primary_category", "suspected_reason",
        "in_pollution_drill", "in_p2p3_alignment", "suggest_retry",
        "backfill_error_type", "backfill_error_message", "backfill_elapsed_s",
        "stock_name", "stock_market", "stock_list_date", "stock_is_active",
        "kline_count", "kline_first_date", "kline_last_date",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    logger.info("Wrote %s (%d rows)", csv_path, len(results))

    # ---- output: category summary ------------------------------------------
    summary = {
        "classified_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "total_codes": len(results),
        "retry_suggested": sum(1 for r in results if r["suggest_retry"]),
        "categories": {cat: by_category.get(cat, 0) for cat in CATEGORIES},
        "category_details": {},
    }
    for cat in CATEGORIES:
        matching = [r for r in results if r["primary_category"] == cat]
        if matching:
            summary["category_details"][cat] = {
                "count": len(matching),
                "codes": [r["code"] for r in matching],
                "sample_reasons": list({r["suspected_reason"] for r in matching}),
            }
    summary["elapsed_seconds"] = round(elapsed, 2)
    summary["peak_rss_bytes"] = tracemalloc.get_traced_memory()[1]
    summary["peak_rss_human"] = rss_human()

    summary_path = output_dir / "category-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote %s", summary_path)

    # ---- final print -------------------------------------------------------
    total_elapsed = time.time() - start_ts
    logger.info("=== classify complete in %.1fs ===", total_elapsed)
    logger.info("Results by category:")
    for cat in CATEGORIES:
        logger.info("  %-30s %d", cat, by_category.get(cat, 0))
    logger.info("Output directory: %s", output_dir)

    sys.stdout = orig_stdout
    sys.stderr = orig_stderr
    log_file_handle.close()

    print(f"\nClassification complete. Output: {output_dir}")
    print(f"  classification-per-code.json")
    print(f"  classification-by-code.csv")
    print(f"  category-summary.json")
    print(f"  run.log")
    print(f"Total codes: {len(results)}")
    print(f"Retry suggested: {sum(1 for r in results if r['suggest_retry'])}")

    return 0


class _TeeIO:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s: str) -> int:
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            st.flush()


if __name__ == "__main__":
    raise SystemExit(main())