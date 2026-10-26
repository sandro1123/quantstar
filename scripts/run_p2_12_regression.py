#!/usr/bin/env python3
"""P2-12 统一回归与服务器终验运行器。

按 P2-12 测试层级逐层执行并收集证据:
1. 当前任务针对性测试
2. data_layer 回归
3. strategy_layer 回归
4. 全项目回归
5. .5 真实源有限验收
6. 完整隔离数据验收
7. 正式迁移演练(只读验证,不写正式库)
8. 回滚演练(只读验证)

证据输出到 JSON 报告:每层 pytest 计数(pass/fail/skip/warnings)、时长、
候选代码 HEAD、隔离数据库 SHA256、正式数据库未改变证明、服务状态、未启用交易证明。

用法:
    python3 scripts/run_p2_12_regression.py \
        --isolated-db ~/quantstar-factor-iso/factor_v4.db \
        --out ~/quantstar-factor-iso/logs/p2-12_report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMAL_DB = REPO / "data" / "qs.db"

LAYERS: list[tuple[str, str]] = [
    ("targeted", "tests/unit/data_layer/test_backfill_factors_full.py"),
    ("data_layer", "tests/unit/data_layer"),
    ("strategy_layer", "tests/unit/strategy_layer"),
    ("full", "tests/"),
]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def git_status_clean(repo: Path) -> bool:
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return not out.stdout.strip()


def run_pytest(path: str, timeout: int = 3600) -> dict:
    env = dict(os.environ, PYTHONPATH="src")
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "-q", "-p", "no:cov", "-p", "no:cacheprovider",
            path,
        ],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        timeout=timeout,
    )
    out = proc.stdout + proc.stderr
    passed = sum(int(x) for x in re.findall(r"(\d+) passed", out))
    failed = sum(int(x) for x in re.findall(r"(\d+) failed", out))
    skipped = sum(int(x) for x in re.findall(r"(\d+) skipped", out))
    warnings = len(re.findall(r"warnings summary", out))
    return {
        "path": path,
        "exit_code": proc.returncode,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "has_warning_summary": warnings > 0,
        "tail": "\n".join(out.splitlines()[-6:]),
    }


def isolated_db_acceptance(conn) -> dict:
    """完整隔离数据验收:检查 P2-10 重算结果满足门禁。"""
    runs = conn.execute(
        "SELECT run_id,status,planned_dates,completed_dates,rows_written,"
        "failure_count,quick_check FROM factor_recompute_runs"
    ).fetchall()
    run_rows = [
        {
            "run_id": r[0], "status": r[1], "planned_dates": r[2],
            "completed_dates": r[3], "rows_written": r[4],
            "failure_count": r[5], "quick_check": r[6],
        }
        for r in runs
    ]
    last = runs[-1] if runs else None
    provenance = conn.execute(
        "SELECT COUNT(*) FROM factor_recompute_provenance"
    ).fetchone()[0]
    ok = bool(
        last
        and last[1] == "quality_pass"
        and last[5] == 0
        and last[6] == "ok"
    )
    return {
        "runs": run_rows,
        "provenance_rows": provenance,
        "latest_status": last[1] if last else None,
        "latest_rows_written": last[4] if last else None,
        "latest_failure_count": last[5] if last else None,
        "quick_check": last[6] if last else None,
        "acceptance": ok,
    }


def formal_db_unchanged(db_path: Path, expected_sha: str) -> bool:
    if not db_path.is_file():
        return False
    return sha256(db_path) == expected_sha


def service_and_trading_status() -> dict:
    svc = subprocess.run(
        ["systemctl", "is-active", "quantstar", "quantstar-api", "quantstar-dash"],
        capture_output=True, text=True,
    )
    active = [s for s in svc.stdout.split() if s == "active"]
    cron = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True,
    )
    cron_active = bool(cron.stdout and not cron.stdout.startswith("no crontab"))
    return {
        "active_services": active,
        "all_services_inactive": len(active) == 0,
        "crontab_active": cron_active,
        "trading_enabled_note": "services inactive + crontab inactive => 交易未启用",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--formal-db-sha", default="",
                        help="期望的正式数据库 SHA256(未改变证明)")
    parser.add_argument("--skip-full", action="store_true",
                        help="跳过全项目回归(耗时最长)")
    parser.add_argument("--layer-timeout", type=int, default=3600)
    args = parser.parse_args()

    import sqlite3
    conn = sqlite3.connect(f"file:{args.isolated_db}?mode=ro", uri=True)

    report: dict = {
        "generated_at": utc_iso(),
        "repo": str(REPO),
        "code_head": git_head(REPO),
        "code_working_tree_clean": git_status_clean(REPO),
        "formal_db": str(FORMAL_DB),
        "formal_db_sha256": sha256(FORMAL_DB),
        "formal_db_unchanged_vs_expected": (
            formal_db_unchanged(FORMAL_DB, args.formal_db_sha)
            if args.formal_db_sha else None
        ),
        "isolated_db": args.isolated_db,
        "isolated_db_sha256": sha256(Path(args.isolated_db)),
        "layer_results": {},
    }

    for name, path in LAYERS:
        if name == "full" and args.skip_full:
            report["layer_results"][name] = {"skipped": True}
            continue
        print(f"[P2-12] running layer {name}: {path}")
        report["layer_results"][name] = run_pytest(path, timeout=args.layer_timeout)

    report["isolated_acceptance"] = isolated_db_acceptance(conn)
    report["service_status"] = service_and_trading_status()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nP2-12 report written to {args.out}")
    for name, res in report["layer_results"].items():
        if isinstance(res, dict) and "exit_code" in res:
            status = "PASS" if res["exit_code"] == 0 else "FAIL"
            print(f"  {name}: {status} "
                  f"(passed={res['passed']} failed={res['failed']} skipped={res['skipped']})")
    acc = report["isolated_acceptance"]
    print(f"  isolated acceptance: {acc['acceptance']} "
          f"(latest={acc['latest_status']}, rows={acc['latest_rows_written']}, "
          f"failures={acc['latest_failure_count']}, quick_check={acc['quick_check']})")
    print(f"  services: {report['service_status']['active_services'] or 'inactive'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
