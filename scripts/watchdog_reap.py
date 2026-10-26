#!/usr/bin/env python3
"""P0-7/G3: watchdog 杀进程后收尸 pipeline_runs running 行。

调用方 = scripts/cron_watchdog.sh 的 RC=124 分支 (SIGTERM->SIGKILL 后)。
语义强于 PipelineLogger.reap_expired_runs 的超时推定: 进程确定死亡,
故对指定 key 当日 running 行无条件收尸 (UPDATE failed + 审计 note, 永不 DELETE)。
flock 互斥 (GO-7) 保证同 key 单实例运行, 无误伤并发活体。

用法:
  PYTHONPATH=src .venv/bin/python scripts/watchdog_reap.py <key> [date]
  date 缺省 = 今天 (BJ)。收不到行也 exit 0 — 收尸是尽力而为的善后,
  不改变 watchdog 自身的退出语义。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_AUDIT_NOTE = "reaped: watchdog killed process (RC=124), cron_watchdog 善后"


def reap(conn, key: str, today: str, now_str: str) -> list[str]:
    """无条件收尸 key 当日 running 行。返回被收的 run_id 列表。"""
    rows = conn.execute(
        "SELECT run_id FROM pipeline_runs "
        "WHERE pipeline_name=? AND date=? AND status='running'",
        (key, today),
    ).fetchall()
    reaped = []
    for row in rows:
        cur = conn.execute(
            "UPDATE pipeline_runs SET status='failed', error_msg=?, end_time=? "
            "WHERE run_id=? AND status='running'",  # 二次守卫
            (_AUDIT_NOTE, now_str, row["run_id"]),
        )
        if cur.rowcount:
            reaped.append(row["run_id"])
    conn.commit()
    return reaped


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: watchdog_reap.py <key> [date YYYY-MM-DD]", file=sys.stderr)
        return 2
    key = argv[1]
    today = argv[2] if len(argv) > 2 else datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from common.database import Database

    conn = Database().get_connection()
    reaped = reap(conn, key, today, now_str)
    if reaped:
        for run_id in reaped:
            print(f"[watchdog_reap] {run_id} -> failed (audit note)", file=sys.stderr)
    else:
        print(f"[watchdog_reap] {key} {today}: no running rows to reap", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
