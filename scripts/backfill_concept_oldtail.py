#!/usr/bin/env python3
"""concept 无偏回补 旧尾段 (walk_forward/backtest 消费段之前) — 2024-01-02 ~ 2026-04-29

接手 B 阵亡任务 (统筹 sm-b06e673948dc4f2d)。仿 backfill_concept_seg1.py。
只回补 rank 探明清单内可用日期 (旧尾 561 天全可用, 无空洞; 4 空洞全在 seg1
2026 五一段)。不用 plates_list 兜底防前视。

用法:
  隔离彩排: python scripts/backfill_concept_oldtail.py --db /tmp/cboldtail_isolated.db
  生产执行: python scripts/backfill_concept_oldtail.py --db data/qs.db --prod
            (生产须先走备份三步曲, 22:05窗口后, 统筹授权)
  小样冒烟: python scripts/backfill_concept_oldtail.py --db /tmp/x.db --limit 2
  增量续跑: python scripts/backfill_concept_oldtail.py --db data/qs.db --prod --skip-existing
            (08-29 增量守卫: 跳过已标记完成的天, 断点续跑; 见下)

增量守卫 (--skip-existing, 08-29 统筹派单):
  背景: 全量 561 天 ≈14h 且 flock_guard 持锁整个脚本时长, 挂 cron 会造成
  晨间作业 mass-skip 死循环 (08-29 应急摘除 crontab 的事故根因)。
  完成一天才原子记进度 (concept_backfill_progress 小表, 一天一事务);
  **只跳有完成标记的天** — 中途崩溃留下的部分写入天 (有行无标记) 必须重跑,
  不能只看「有行就跳」。幂等靠 INSERT OR REPLACE, 重跑一天无副作用。
  默认行为不变: 不加 flag = 全量重跑现状语义。

逐批指纹断言: 每 N 日落库后核对 COUNT(DISTINCT concept_name)/source后缀/_backfill标记。
"""
import argparse, sqlite3, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.concept_plates import ConceptPlatesPipeline

PLAN_START = "2024-01-02"
PLAN_END = "2026-04-29"
PROBE_CSV = "/tmp/rank_probe_results.csv"
BATCH_SIZE = 10  # 每批指纹断言

# 兼容旧名 (外部如有引用)
OLD_TAIL_START = PLAN_START
OLD_TAIL_END = PLAN_END

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS concept_backfill_progress (
    date TEXT PRIMARY KEY,
    completed_at TEXT DEFAULT (datetime('now'))
)
"""


def load_available_dates():
    # 注意: 读模块全局 PROBE_CSV 须在调用时解析 (def 默认参数在定义时绑定,
    # monkeypatch 模块属性不生效 — 561 天真 CSV 闯进单测的事故)
    avail = []
    holes = []
    with open(PROBE_CSV) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 3 or parts[0] == "date":
                continue
            d, r15, r17 = parts[0], int(parts[1]), int(parts[2])
            if not (PLAN_START <= d <= PLAN_END):
                continue
            if r15 > 0 or r17 > 0:
                avail.append(d)
            else:
                holes.append(d)
    return avail, holes


def _ensure_progress_table(conn):
    conn.execute(PROGRESS_DDL)
    conn.commit()


def mark_day_done(conn, date):
    """原子记一天完成 — 与该日 concept_daily 写入不同事务也不受影响.

    完成标记是「重跑跳过」的唯一依据; 部分写入天 (有行无标记) 会被重跑,
    幂等靠 INSERT OR REPLACE 保证重跑无副作用。
    """
    _ensure_progress_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO concept_backfill_progress (date) VALUES (?)",
        (date,))
    conn.commit()


def load_completed_days(conn):
    """只认完成标记, 不看 concept_daily 有没有行 (防「有行就跳」误跳部分写入天)."""
    try:
        _ensure_progress_table(conn)
        rows = conn.execute(
            "SELECT date FROM concept_backfill_progress").fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def drift_guard(conn, completed):
    """漂移安全网 (D 审查改进①): 标记天若 concept_daily 零行 = 进度与数据漂移,
    该天降级待跑 + WARNING 点名 (不静默 — 铁律 #12).

    一条联查 SQL, 不动跳过行为本身: 只把「标记但零行」的天从已完成集合剔除。
    (成因: 表被清/写入丢失/人工误播种。零行天重跑幂等无副作用。)
    """
    if not completed:
        return completed
    rows = conn.execute(
        "SELECT p.date, (SELECT COUNT(*) FROM concept_daily c "
        " WHERE c.date = p.date) AS n "
        "FROM concept_backfill_progress p WHERE p.date IN (%s)"
        % ",".join("?" * len(completed)), tuple(sorted(completed))).fetchall()
    drifted = {r[0] for r in rows if r[1] == 0}
    if drifted:
        print("WARNING drift_guard: 完成标记天零行 (降级待跑): %s" % sorted(drifted),
              flush=True)
    return completed - drifted


def fingerprint(conn, date):
    n = conn.execute("SELECT COUNT(*) FROM concept_daily WHERE date=? AND source LIKE ?",
                     (date, "%_backfill%")).fetchone()[0]
    nc = conn.execute("SELECT COUNT(DISTINCT concept_name) FROM concept_daily WHERE date=?",
                      (date,)).fetchone()[0]
    return n, nc


def run_backfill(db_path, pipe, skip_existing=False, sleep_s=0.05, limit=0,
                 batch_size=BATCH_SIZE):
    """核心回补循环 (从 main 抽出可测).

    db_path: 目标库路径; pipe: ConceptPlatesPipeline 或测试替身 (须 .run(PipelineInput)).
    skip_existing: True 时跳过有完成标记的天; False 时全量重跑 (现状语义).
    返回 {"total","skip","redo","ok","fail"}.
    """
    avail, holes = load_available_dates()
    print("OLDTAIL available=%d holes=%d %s" % (len(avail), len(holes), holes),
          flush=True)
    if limit > 0:
        avail = avail[:limit]
        print("LIMIT mode: 只跑前 %d 天" % len(avail), flush=True)

    db = Database(db_path)
    conn = sqlite3.connect(db_path)
    completed = load_completed_days(conn) if skip_existing else set()
    completed = drift_guard(conn, completed)  # D 改进①: 标记天零行降级待跑

    # 增量选日: 完成标记天跳过, 部分写入天 (有行无标记) 与全空天都重跑
    todo = [d for d in avail if d not in completed]
    skipped = len(avail) - len(todo)
    print("SKIP-EXISTING mode: skip=%d redo=%d" % (skipped, len(todo)), flush=True)

    ok = fail = 0
    for i, d in enumerate(todo):
        res = pipe.run(PipelineInput(date=d, db=db,
                                     config={"backfill": True, "sleep_s": sleep_s}))
        if res.status == "success":
            ok += 1
            mark_day_done(conn, d)  # 完成一天才记进度 (失败日不记, 下次重跑)
        else:
            fail += 1
            print("FAIL %s: %s" % (d, res.errors[:1]), flush=True)
        if (i + 1) % batch_size == 0:
            n, nc = fingerprint(conn, d)
            print("BATCH %d/%d date=%s written=%s concepts=%s ok=%d fail=%d" % (
                i + 1, len(todo), d, n, nc, ok, fail), flush=True)
        time.sleep(sleep_s)

    conn.close()
    stats = {"total": len(avail), "skip": skipped, "redo": len(todo),
             "ok": ok, "fail": fail}
    print("OLDTAIL_DONE total=%d skip=%d redo=%d ok=%d fail=%d" % (
        len(avail), skipped, len(todo), ok, fail), flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--prod", action="store_true", help="生产库标记 (须先备份三步曲)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="增量续跑: 只跳有完成标记的天 (08-29 增量守卫; "
                         "部分写入天重跑, 不加本 flag = 全量重跑现状语义)")
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 天 (冒烟用, 0=全量)")
    args = ap.parse_args()

    avail, holes = load_available_dates()
    print("OLDTAIL available=%d holes=%d %s" % (len(avail), len(holes), holes),
          flush=True)
    if args.prod:
        print("PROD MODE — 确认已完成备份三步曲", flush=True)

    pipe = ConceptPlatesPipeline()
    run_backfill(
        db_path=args.db, pipe=pipe, skip_existing=args.skip_existing,
        sleep_s=args.sleep, limit=args.limit)


if __name__ == "__main__":
    main()
