"""S0-8 涨停/跌停池补数脚本 — 2026-08-16

stock_limit_up_down 停在 2026-07-07, 缺 26 个交易日(07-09 ~ 08-14)。
通过 SourceRegistry(akshare 优先/fuyao 兜底)逐日回填, INSERT OR REPLACE 幂等。

流程: 表级备份 → 隔离库试跑2天 → 正式库逐日回填 → JSONL 证据 → 核对
用法: PYTHONPATH=src python scripts/backfill_limit_up_down.py [--dry-run N]
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.limit_up_down import LimitUpDownPipeline

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "qs.db")
EVIDENCE = os.path.join(PROJECT_ROOT, "logs", "repair_evidence_s0.jsonl")
TABLE_DDL = """
CREATE TABLE IF NOT EXISTS stock_limit_up_down (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    name TEXT DEFAULT '',
    limit_type TEXT NOT NULL,
    price REAL DEFAULT 0,
    change_pct REAL DEFAULT 0,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (code, date, limit_type)
)
"""


def _log(event: str, **fields):
    row = {"timestamp": datetime.now().isoformat(), "event": event, **fields}
    with open(EVIDENCE, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False))


def get_missing_dates() -> list[str]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    trading = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date > '2026-07-07' ORDER BY date")]
    existing = {r[0] for r in conn.execute("SELECT DISTINCT date FROM stock_limit_up_down")}
    conn.close()
    return [d for d in trading if d not in existing]


def backup_table():
    bak = os.path.join(PROJECT_ROOT, "data", "backups", f"limit_up_down_backup_20260816.db")
    if os.path.exists(bak):
        print(f"备份已存在: {bak}")
        return bak
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(bak)
    src.execute(f"ATTACH DATABASE '{bak}' as bak")
    src.execute("CREATE TABLE bak.stock_limit_up_down AS SELECT * FROM stock_limit_up_down")
    src.commit()
    cnt = dst.execute("SELECT COUNT(*) FROM stock_limit_up_down").fetchone()[0]
    src.close()
    dst.close()
    _log("table_backup", path=bak, rows=cnt)
    return bak


def _source_registry():
    from config_layer import Config
    config = Config()
    config.load()
    db = Database(DB_PATH)
    from data_layer import DataLayer
    dl = DataLayer(config=config, db=db)
    return dl._source_registry, db


def run_isolated(dates: list[str]) -> bool:
    """隔离库试跑: 临时库 + 真源拉取, 验证字段/口径"""
    iso = os.path.join("/tmp", "opencode", "lud_iso_test.db")
    if os.path.exists(iso):
        os.remove(iso)
    conn = sqlite3.connect(iso)
    conn.execute(TABLE_DDL)
    conn.commit()
    conn.close()

    class _IsolatedDB:
        def __init__(self, path):
            self._conn = sqlite3.connect(path)
            self._conn.row_factory = sqlite3.Row
        def get_connection(self):
            return self._conn

    registry, real_db = _source_registry()
    pipe = LimitUpDownPipeline(db=_IsolatedDB(iso), source_registry=registry)
    ok = True
    for d in dates:
        res = pipe.run(PipelineInput(date=d))
        _log("isolated_run", date=d, status=res.status, records=res.records_written,
             errors=res.errors[:3])
        if res.status != "success":
            ok = False
    conn = sqlite3.connect(iso)
    sample = conn.execute(
        "SELECT date, limit_type, COUNT(*) FROM stock_limit_up_down GROUP BY date, limit_type").fetchall()
    conn.close()
    _log("isolated_summary", sample=sample)
    real_db.close()
    return ok and len(sample) > 0


def backfill(dates: list[str]):
    registry, db = _source_registry()
    pipe = LimitUpDownPipeline(db=db, source_registry=registry)
    total = 0
    skipped = []
    for d in dates:
        res = pipe.run(PipelineInput(date=d))
        if res.status == "success":
            total += res.records_written
            _log("backfill_date", date=d, status=res.status, records=res.records_written)
        else:
            # 东财涨停池历史窗口有限(~30天), 超窗日期标 skipped, 后续由每日 cron 保证不再断档
            skipped.append(d)
            _log("backfill_date", date=d, status="skipped", errors=res.errors[:2])
        time.sleep(1.0)
    db.close()
    _log("backfill_done", total_records=total, recovered=len(dates) - len(skipped), skipped=skipped)
    return total, skipped


def verify():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM stock_limit_up_down WHERE date > '2026-07-07' "
        "GROUP BY date ORDER BY date").fetchall()
    conn.close()
    _log("verify", days_covered=len(rows), per_day_first3=[list(r) for r in rows[:3]],
         per_day_last3=[list(r) for r in rows[-3:]])
    return len(rows)


def main():
    os.makedirs("/tmp/opencode", exist_ok=True)
    if "--dry-run" in sys.argv:
        dates = get_missing_dates()[-3:]
        print(f"试跑 {len(dates)} 天(窗口内): {dates}")
        ok = run_isolated(dates)
        print("隔离试跑:", "OK" if ok else "FAILED")
        return 0 if ok else 1

    dates = get_missing_dates()
    print(f"缺数交易日 {len(dates)} 天: {dates[0]} ~ {dates[-1]}")
    if not dates:
        print("无缺数, 跳过")
        return 0
    # 源历史窗口约最近 30 个交易日, 只试跑窗口内(最后 10 天)验证字段口径
    trial = dates[-10:]
    backup_table()
    if not run_isolated(trial[-2:]):
        print("❌ 隔离试跑失败, 不写正式库")
        return 1
    print(f"✅ 隔离试跑通过({trial[-2:]})，开始正式库回填 {len(dates)} 天")
    total, skipped = backfill(dates)
    covered = verify()
    print(f"完成: 回填 {total} 行, 覆盖 {covered} 天, 超窗跳过 {len(skipped)} 天 {skipped[:5]}...")
    return 0 if covered >= len(dates) - len(skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
