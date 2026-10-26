#!/usr/bin/env python
"""D1-b 清创脚本: backfill_announce_date 病行族清创 (--dry-run / --apply)。

定谳 (2026-09-12 凌晨取证):
- 病面两类: ①dup 5358 行 (06-29 病键 + 06-30 标准键双行共存, 值全同,
  对拍 5357/5358 announce 差恰 +1; 特例 1 行 002762 差 9 = 公告修正二次写,
  以标准行为准) → 清创=**DELETE 病键行** (标准键行已是修法① BJT 真值)。
  ②orphan 5381 行 (03-30 键 5193 + 06-29 键 188: 病键存在但无标准键对应
  — Q1 批全 UTC 键写 / Q2 新增股只被病键写过) → 清创=**平移修正**
  (period+1=标准键, announce+1=BJT 真值; ms_to_date UTC→BJT 的差恒 +1 天,
  数学上确定无需网络)。
- 2025-09-30 之前的碎屑族 (每期 1~5 只) 键形态正常 (标准季末日), 不在本卡面
  (D0 处置案 A 候裁)。

幂等: DELETE 按 (code, 病键 date) 精确; 平移 = 先 INSERT OR IGNORE 标准键行
(值从病行拷, announce+1) 再 DELETE 病行 — 标准键已有行 (并发写) 时跳过
INSERT 保留现值, 病行仍删。
S0: --apply 前单表 dump 先行 (调用方负责; 脚本 --backup 亦可用)。
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta

OFF_BY_ONE_MMDD = {"03-30", "06-29", "09-29", "12-30"}


def classify(conn: sqlite3.Connection) -> dict:
    """病行分类 (只读)。返回 {dup: [...], orphan: [...]}。"""
    rows = conn.execute(
        "SELECT code, date, announce_date FROM stock_financials ORDER BY code, date"
    ).fetchall()
    keys = {(r["code"], r["date"]) for r in rows}
    dup, orphan = [], []
    for r in rows:
        if r["date"][5:] not in OFF_BY_ONE_MMDD:
            continue
        std = (datetime.strptime(r["date"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        if (r["code"], std) in keys:
            dup.append((r["code"], r["date"]))
        else:
            orphan.append((r["code"], r["date"], r["announce_date"] or ""))
    return {"dup": dup, "orphan": orphan}


def _shift(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def run_cleanse(conn: sqlite3.Connection, dry_run: bool = True) -> dict:
    """清创主流程。dry 只出计划; apply 执行 DELETE + 平移。"""
    c = classify(conn)
    dup, orphan = c["dup"], c["orphan"]
    plan = {
        "n_delete_dup": len(dup),
        "n_shift_orphan": len(orphan),
        "dup_sample": dup[:3],
        "orphan_sample": [(co, d, "->", _shift(d), "| ann", a, "->", _shift(a) if a else "")
                          for co, d, a in orphan[:3]],
    }
    if dry_run:
        return plan
    # dup: 删病键行 (标准键行保留)
    for code, d in dup:
        conn.execute("DELETE FROM stock_financials WHERE code = ? AND date = ?", (code, d))
    # orphan: 平移 — 拷行到标准键 (值全拷, announce+1), 再删病行
    for code, d, ann in orphan:
        row = conn.execute(
            "SELECT * FROM stock_financials WHERE code = ? AND date = ?", (code, d)).fetchone()
        if row is None:
            continue
        new_ann = _shift(ann) if ann else ""
        conn.execute(
            "INSERT OR IGNORE INTO stock_financials "
            "(code, date, eps, roe, revenue, profit, total_assets, total_liabilities, "
            " net_assets, operating_cash_flow, gross_margin, net_margin, announce_date, caliber) "
            "SELECT code, ?, eps, roe, revenue, profit, total_assets, total_liabilities, "
            " net_assets, operating_cash_flow, gross_margin, net_margin, ?, caliber "
            "FROM stock_financials WHERE code = ? AND date = ?",
            (_shift(d), new_ann, code, d))
        conn.execute("DELETE FROM stock_financials WHERE code = ? AND date = ?", (code, d))
    conn.commit()
    return plan


def main():
    p = argparse.ArgumentParser(description="D1-b: announce_date off-by-one 病行清创")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--db", default="data/qs.db")
    args = p.parse_args()
    print(f"DB: {args.db}  Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    if args.apply:
        conn = sqlite3.connect(args.db)
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    r = run_cleanse(conn, dry_run=not args.apply)
    print(f"plan: delete_dup={r['n_delete_dup']} shift_orphan={r['n_shift_orphan']}")
    print(f"dup_sample: {r['dup_sample']}")
    print(f"orphan_sample: {r['orphan_sample']}")
    conn.close()


if __name__ == "__main__":
    main()
