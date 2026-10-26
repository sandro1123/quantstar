"""OPS-01: 存量僵尸 running 记录收割脚本

扫描 pipeline_runs 中 status='running' 且 start_time 早于
now - (timeout_s + 宽限 300s) 的记录, 收为 failed (error_msg 标 zombie reaped)。

默认 dry-run 只打印; --apply 执行收割; --backup 先把 pipeline_runs 备份到
pipeline_runs_backup_<ts> 表 (写库三步曲: 备份→执行→验证)。

用法:
  PYTHONPATH=src .venv/bin/python scripts/reap_zombie_runs.py            # dry-run
  PYTHONPATH=src .venv/bin/python scripts/reap_zombie_runs.py --apply --backup
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

_GRACE_S = 300
_DEFAULT_TIMEOUT_S = 300


def main() -> int:
    parser = argparse.ArgumentParser(description="收割僵尸 running 记录")
    parser.add_argument("--apply", action="store_true", help="执行收割 (默认 dry-run)")
    parser.add_argument("--backup", action="store_true", help="收割前备份 pipeline_runs 表")
    args = parser.parse_args()

    from common.database import Database
    from common.utils import bj_now
    from ops_layer.core.tables import OPS_TABLES_DDL

    db = Database()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()

    # 管道 timeout 从 ops.yaml 读 (与 orchestrator 同源)
    timeout_lookup: dict[str, int] = {}
    try:
        from config_layer import cfg_get
        pipelines = cfg_get("ops.pipelines") or {}
        for name, cfg in pipelines.items():
            if isinstance(cfg, dict):
                timeout_lookup[name] = int(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
    except Exception as e:
        print(f"⚠️ 管道配置读取失败, 全部用默认 timeout {_DEFAULT_TIMEOUT_S}s: {e}")

    now = datetime.now()
    rows = conn.execute(
        "SELECT run_id, pipeline_name, start_time FROM pipeline_runs WHERE status='running'"
    ).fetchall()
    zombies = []
    for row in rows:
        try:
            start = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        timeout_s = timeout_lookup.get(row["pipeline_name"], _DEFAULT_TIMEOUT_S)
        if (now - start).total_seconds() > timeout_s + _GRACE_S:
            zombies.append((dict(row), timeout_s))

    print(f"running 记录总数: {len(rows)}, 判定僵尸: {len(zombies)}")
    for z, timeout_s in zombies:
        print(f"  🧟 {z['run_id']}  start={z['start_time']}  (timeout {timeout_s}s + 宽限 {_GRACE_S}s)")

    if not zombies:
        print("无需收割。")
        return 0
    if not args.apply:
        print("\n[dry-run] 未执行。确认后加 --apply --backup 执行。")
        return 0

    if args.backup:
        ts = now.strftime("%Y%m%d_%H%M%S")
        backup_table = f"pipeline_runs_backup_{ts}"
        conn.execute(f"CREATE TABLE {backup_table} AS SELECT * FROM pipeline_runs")
        conn.commit()
        n = conn.execute(f"SELECT COUNT(*) FROM {backup_table}").fetchone()[0]
        print(f"✅ 备份完成: {backup_table} ({n} 行)")

    for z, _t in zombies:
        conn.execute(
            "UPDATE pipeline_runs SET status='failed', "
            "error_msg='zombie reaped: 存量僵尸记录由 scripts/reap_zombie_runs.py 收割', end_time=? "
            "WHERE run_id=?",
            (bj_now(), z["run_id"]),
        )
    conn.commit()

    # 验证
    remaining = conn.execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE status='running'"
    ).fetchone()[0]
    print(f"✅ 收割完成: {len(zombies)} 条 → failed; 剩余 running: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
