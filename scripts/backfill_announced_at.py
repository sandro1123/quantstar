"""D1 PIT 第一段: stock_financials 加 announced_at 列 + 保守回填。

架构定谳 (probe 2026-09-11):
- 生产已有 announce_date (DL-47, fuyao 真值, 10740/10803 行) — 真值优先。
- 剩余 63 行 announce_date 为空 (fuyao 12 季窗外/源端确无, 集中在
  2026-03-30~2018 旧报告期) — 旧行为: _loader PIT 过滤把空行放行
  (= 保守前视风险窗口)。
- 本脚本加 announced_at 列: 真值行 = announce_date; 空行 = 报告期末
  +45 天保守上界 (D1 设计定谳口径)。45 天选择: fuyao 实测 lag 分布
  10-30 天 27.2%, 30-45 天 7%, 45-60 天 54%, 60-90 天 11.5% —
  45 天是「宁可晚见不早见」的保守界, 保证 announced_at >= 任何可能
  真实公告日下界, 永不前视。
- PIT 读面 (_loader._get_financials_range) 从 announce_date 切到
  announced_at: 空真值行不再放行, 走保守上界可见性 (新数据可见性
  判定; 存量已算因子不重刷 — 非本卡面)。

幂等: announced_at 计算为纯函数 (date, announce_date) → 恒等; 重跑
UPDATE 同值零漂移。dry-run 只读出计划 (不建列不写), --apply 建列
(守卫 ALTER) + 执行计划。
三步曲用法与 backfill_announce_date.py 同族:
  --dry-run 只读;  隔离库 --apply 验证;  生产 --apply 需统筹 GO。
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta

CONSERVATIVE_LAG_DAYS = 45

ALTER_SQL = "ALTER TABLE stock_financials ADD COLUMN announced_at TEXT DEFAULT ''"


def compute_announced_at(period_end: str, announce_date: str) -> str:
    """(报告期末, 真公告日) → announced_at。纯函数, 幂等。

    真值优先: announce_date 非空直接用 (真实公告日是最准确的 PIT 界)。
    空则保守: 期末 + 45 天 (CONSERVATIVE_LAG_DAYS)。日期非法 → ''。
    """
    if announce_date:
        return announce_date
    try:
        d = datetime.strptime(period_end, "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""
    return (d + timedelta(days=CONSERVATIVE_LAG_DAYS)).strftime("%Y-%m-%d")


def _has_column(conn: sqlite3.Connection, col: str) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_financials)").fetchall()}
    return col in cols


def run_backfill(conn: sqlite3.Connection, dry_run: bool = True) -> dict:
    """回填主流程。返回计划/执行面: {n_update, from_real, from_conservative, plan}。

    plan = [(code, date, announced_at)] 升序 — dry 与 apply 共用同一
    计算路径, 恒等对拍由测试保证 (dry["plan"] == wet["plan"])。
    dry_run=True: 纯只读 (不建列不写) — PRAGMA-first, 库内缺
    announced_at 列时直接按 announce_date 列算计划。
    dry_run=False: 守卫 ALTER 建列 → UPDATE → commit。
    """
    rows = conn.execute(
        "SELECT code, date, announce_date FROM stock_financials ORDER BY code, date"
    ).fetchall()
    plan = []
    from_real = 0
    from_conservative = 0
    for r in rows:
        val = compute_announced_at(r["date"], r["announce_date"] or "")
        if not val:
            continue  # 日期非法行不造假 — 诚实留空
        plan.append((r["code"], r["date"], val))
        if r["announce_date"]:
            from_real += 1
        else:
            from_conservative += 1

    result = {
        "n_update": len(plan),
        "from_real": from_real,
        "from_conservative": from_conservative,
        "plan": plan,
    }
    if dry_run:
        return result

    if not _has_column(conn, "announced_at"):
        conn.execute(ALTER_SQL)
    conn.executemany(
        "UPDATE stock_financials SET announced_at = ? WHERE code = ? AND date = ?",
        [(v, c, d) for c, d, v in plan])
    conn.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description="D1 PIT: announced_at 保守回填")
    parser.add_argument("--apply", action="store_true", help="实际写库 (默认 dry-run 只读)")
    parser.add_argument("--db", default="data/qs.db", help="目标库")
    args = parser.parse_args()
    db_path = args.db
    print(f"DB: {db_path}  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    if args.apply:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    r = run_backfill(conn, dry_run=not args.apply)
    print(f"plan rows={r['n_update']} from_real={r['from_real']} "
          f"from_conservative={r['from_conservative']}")
    if r["plan"]:
        print(f"sample: {r['plan'][:5]}")
    conn.close()


if __name__ == "__main__":
    main()
