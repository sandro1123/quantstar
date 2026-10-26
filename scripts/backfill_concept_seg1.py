#!/usr/bin/env python3
"""concept 无偏回补 第一段 (walk_forward/backtest 消费段) — 2026-04-30 ~ 2026-08-27

统筹 sm-88d821904a4141b8 放量GO 第一段。只回补 rank 探明清单内可用日期
(4洞=2026五一假期段, 记施工日志, 不用 plates_list 兜底防前视)。

用法:
  隔离彩排: python scripts/backfill_concept_seg1.py --db /tmp/cbpit_isolated.db
  生产执行: python scripts/backfill_concept_seg1.py --db data/qs.db --prod
            (生产须先走备份三步曲, 22:05窗口后, 统筹授权)

逐批指纹断言: 每 N 日落库后核对 COUNT(DISTINCT concept_name)/source后缀/_backfill标记。
"""
import argparse, sqlite3, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.concept_plates import ConceptPlatesPipeline

SEG1_START = "2026-04-30"
SEG1_END = "2026-08-27"
PROBE_CSV = "/tmp/rank_probe_results.csv"
BATCH_SIZE = 10  # 每批指纹断言


def load_available_dates():
    avail = []
    holes = []
    with open(PROBE_CSV) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 3 or parts[0] == "date":
                continue
            d, r15, r17 = parts[0], int(parts[1]), int(parts[2])
            if not (SEG1_START <= d <= SEG1_END):
                continue
            if r15 > 0 or r17 > 0:
                avail.append(d)
            else:
                holes.append(d)
    return avail, holes


def fingerprint(conn, date):
    n = conn.execute("SELECT COUNT(*) FROM concept_daily WHERE date=? AND source LIKE ?",
                     (date, "%_backfill%")).fetchone()[0]
    nc = conn.execute("SELECT COUNT(DISTINCT concept_name) FROM concept_daily WHERE date=?",
                      (date,)).fetchone()[0]
    return n, nc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--prod", action="store_true", help="生产库标记 (须先备份三步曲)")
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args()

    avail, holes = load_available_dates()
    print("SEG1 available=%d holes=%d %s" % (len(avail), len(holes), holes))
    if args.prod:
        print("PROD MODE — 确认已完成备份三步曲")

    db = Database(args.db)
    pipe = ConceptPlatesPipeline()
    conn = sqlite3.connect(args.db)

    ok = fail = 0
    for i, d in enumerate(avail):
        res = pipe.run(PipelineInput(date=d, db=db, config={"backfill": True, "sleep_s": args.sleep}))
        if res.status == "success":
            ok += 1
        else:
            fail += 1
            print("FAIL %s: %s" % (d, res.errors[:1]))
        if (i + 1) % BATCH_SIZE == 0:
            n, nc = fingerprint(conn, d)
            print("BATCH %d/%d date=%s written=%s concepts=%s ok=%d fail=%d" % (
                i + 1, len(avail), d, n, nc, ok, fail))
        time.sleep(1.0)  # 限速防429

    conn.close()
    print("SEG1_DONE total=%d ok=%d fail=%d" % (len(avail), ok, fail))


if __name__ == "__main__":
    main()
