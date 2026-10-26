#!/usr/bin/env python3
"""scripts/replay_dlq.py — DL-29 DLQ 重放: 把 logs/dlq/<date>.jsonl 的原始记录重新写入。

用法:
  python scripts/replay_dlq.py --db data/qs.db --file logs/dlq/2026-07-29.jsonl [--dry-run]
  python scripts/replay_dlq.py --db data/qs.db --date 2026-07-29 [--dry-run]

DLQ 记录含完整原始 record, 重放即 batch_upsert 回 daily_klines
(重放会再过一遍质量闸 — 若数据仍坏会再次标记, 不会静默)。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from common.database import Database
from data_layer.repos.kline import KlineRepo


def main() -> int:
    ap = argparse.ArgumentParser(description="重放 DLQ 记录")
    ap.add_argument("--db", required=True, help="目标 SQLite 库路径")
    ap.add_argument("--file", help="DLQ jsonl 文件路径")
    ap.add_argument("--date", help="按日期找 logs/dlq/<date>.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    args = ap.parse_args()

    if args.file:
        path = Path(args.file)
    elif args.date:
        root = Path(__file__).resolve().parent.parent
        path = root / "logs" / "dlq" / f"{args.date}.jsonl"
    else:
        ap.error("需要 --file 或 --date")
        return 2
    if not path.exists():
        print(f"[replay_dlq] 文件不存在: {path}")
        return 1

    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            rec = ev.get("record")
            if rec:
                records.append(rec)

    print(f"[replay_dlq] {path.name}: {len(records)} 条原始记录")
    if args.dry_run:
        print("[replay_dlq] dry-run: 不写库")
        return 0
    if not records:
        return 0

    repo = KlineRepo(Database(args.db))
    repo.batch_upsert(records)
    print(f"[replay_dlq] replay 完成: {len(records)} 条已写回 daily_klines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
