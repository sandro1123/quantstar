"""OPS-07: 存量 open 告警清理脚本

alerts 表 25 open / 0 resolved — 告警池变垃圾场 (08-11 的告警 open 至今)。
本脚本把超过 N 天 (默认 7) 的 open 告警批量 resolve (error_msg 标 stale cleanup)。

默认 dry-run 只打印; --apply 执行; --backup 先备份 alerts 表
(写库三步曲: 备份→执行→验证)。

用法:
  PYTHONPATH=src .venv/bin/python scripts/cleanup_stale_alerts.py            # dry-run
  PYTHONPATH=src .venv/bin/python scripts/cleanup_stale_alerts.py --apply --backup
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

_STALE_DAYS = 7


def main() -> int:
    parser = argparse.ArgumentParser(description="清理超期 open 告警")
    parser.add_argument("--apply", action="store_true", help="执行清理 (默认 dry-run)")
    parser.add_argument("--backup", action="store_true", help="清理前备份 alerts 表")
    parser.add_argument("--days", type=int, default=_STALE_DAYS, help=f"超期天数阈值 (默认 {_STALE_DAYS})")
    args = parser.parse_args()

    from common.database import Database
    from common.utils import bj_now
    from ops_layer.core.tables import OPS_TABLES_DDL

    db = Database()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()

    cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id, severity, title, created_at FROM alerts "
        "WHERE status='open' AND created_at < ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()

    total_open = conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
    print(f"open 告警总数: {total_open}, 超期 {args.days} 天待清理: {len(rows)}")
    for r in rows:
        print(f"  🔔 #{r['id']} [{r['severity']}] {r['title']}  ({r['created_at']})")

    if not rows:
        print("无需清理。")
        return 0
    if not args.apply:
        print(f"\n[dry-run] 未执行。确认后加 --apply --backup 执行。")
        return 0

    if args.backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_table = f"alerts_backup_{ts}"
        conn.execute(f"CREATE TABLE {backup_table} AS SELECT * FROM alerts")
        conn.commit()
        n = conn.execute(f"SELECT COUNT(*) FROM {backup_table}").fetchone()[0]
        print(f"✅ 备份完成: {backup_table} ({n} 行)")

    now = bj_now()
    for r in rows:
        conn.execute(
            "UPDATE alerts SET status='resolved', resolved_at=?, "
            "action=COALESCE(action,'') || ' | stale cleanup by scripts/cleanup_stale_alerts.py' "
            "WHERE id=?",
            (now, r["id"]),
        )
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
    print(f"✅ 清理完成: {len(rows)} 条 → resolved; 剩余 open: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
