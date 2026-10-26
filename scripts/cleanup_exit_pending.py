"""修复 Blocker 3 — 清理 exit_pending 滞留记录

两类处理:
1. entry_date 非空的真实 exit_pending (7只) → 用最新收盘价结算为 exited
2. entry_date 为 NULL 的脏数据 (66只) → 直接删除 (无入场记录, name 被污染)

运行: PYTHONPATH=src CONFIG_ENV=prod .venv/bin/python scripts/cleanup_exit_pending.py
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from common.database import Database
from common.utils import bj_date

EVIDENCE = os.path.expanduser("~/quantstar-factor-iso/logs/exit_pending_cleanup.jsonl")
os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)


def _log_event(event: str, **fields):
    line = {"timestamp": datetime.now().astimezone().isoformat(), "event": event, **fields}
    with open(EVIDENCE, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(json.dumps(line, ensure_ascii=False))


def main():
    db = Database()
    conn = db.get_connection()
    today = bj_date()

    real = conn.execute(
        "SELECT code, name, entry_price, position_qty FROM stock_pool "
        "WHERE status='exit_pending' AND entry_date IS NOT NULL"
    ).fetchall()
    dirty = conn.execute(
        "SELECT code FROM stock_pool WHERE status='exit_pending' AND entry_date IS NULL"
    ).fetchall()

    print(f"真实 exit_pending: {len(real)} 只, 脏数据: {len(dirty)} 只")

    settled = 0
    for row in real:
        code, name, entry_price, position_qty = row["code"], row["name"], row["entry_price"], row["position_qty"]
        k = conn.execute(
            "SELECT close FROM daily_klines WHERE code=? ORDER BY date DESC LIMIT 1", (code,)
        ).fetchone()
        exit_price = float(k["close"]) if k else float(entry_price or 0)
        total_return = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        proceeds = (position_qty or 0) * exit_price
        conn.execute("""
            UPDATE stock_pool SET
                status='exited', exit_price=?, exit_date=?,
                exit_reason='T+1确认卖出(清理)', exit_reason_detail='exit_pending清理: 最新收盘价结算',
                total_return_pct=?, holding_days_actual=?, exit_low=?,
                updated_at=datetime('now','+8 hours')
            WHERE code=? AND status='exit_pending'
        """, (exit_price, today, round(total_return, 2), 2, exit_price, code))
        _log_event("exit_pending_settled", code=code, name=name, entry_price=entry_price,
                   exit_price=exit_price, total_return_pct=round(total_return, 2),
                   proceeds=round(proceeds, 2))
        settled += 1

    deleted = 0
    for row in dirty:
        code = row["code"]
        conn.execute("DELETE FROM stock_pool WHERE code=? AND status='exit_pending' AND entry_date IS NULL", (code,))
        _log_event("exit_pending_dirty_deleted", code=code)
        deleted += 1

    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM stock_pool WHERE status='exit_pending'").fetchone()[0]
    exited_total = conn.execute("SELECT COUNT(*) FROM stock_pool WHERE status='exited'").fetchone()[0]
    _log_event("cleanup_done", settled=settled, deleted=deleted, remaining_exit_pending=remaining,
               total_exited=exited_total)
    print(f"完成: 结算 {settled} 只, 删除 {deleted} 只, 剩余 exit_pending={remaining}, exited 总数={exited_total}")


if __name__ == "__main__":
    main()