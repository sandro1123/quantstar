#!/usr/bin/env python3
"""DL-35 顺带: 修复 2026-07-29 change_pct=0 批次 (可重算, 无需移交 DL-29)

现状 (只读复核实测): 07-29 总行 5183, change_pct=0 行 5182 (99%), 全部 pre_close>0。
pre_close 与 baostock preclose 一致, close/pre_close 可信 → change_pct 可重算。

⚠️ 2026-08-22 会话C 复核修正 (推翻初版"0 越界"结论):
初版用统一阈值 30.5% 判越界, 漏掉 3 行**坏 close**——它们重算后落在
各自板块涨跌幅限制之外, 物理不可能:
  000990 诚志股份  主板10%  重算 -26.58%
  301172 君逸数码  创业板20% 重算 -24.80%
  688501 青达环保  科创板20% 重算 -27.41%
交叉验证: 这 3 只不在 07-29 除权名单 (baostock query_daily_adjust_factor 12 只
均非此 3 码), 且库内 close 与 baostock raw close 不符 (000990 库内 6.49 vs
baostock 9.08; 库内 6.49 恰等于 baostock 07-30 pre_close) → 库内这 3 行 close
本身写错 (疑似串入次日值), 重算 change_pct 无意义。
处置: 按板块涨跌幅限制判定, 排除这 3 行坏 close (不重算, 移交 DL-29 标记异常),
重算其余 5179 行。

写库三步曲: 只读验证 → 备份 → 隔离验证 → GO。

用法:
  1. 备份: cp data/qs.db data/backups/qs_pre_changepct_fix_<date>.db
  2. 隔离验证: python scripts/fix_changepct_20260729.py --db <隔离库> --dry-run
  3. 正式执行 (需 GO): python scripts/fix_changepct_20260729.py --db data/qs.db --apply
证据: 追加 logs/repair_evidence_dl35.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "daily_klines"
TARGET_DATE = "2026-07-29"


def board_limit(code: str) -> float:
    """按板块返回涨跌幅限制 (%)。创业板/科创板 20, 北交所 30, 主板 10。"""
    c = str(code).split(".")[0]
    if c.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    if c.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 07-29 change_pct=0 批次")
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", default=TARGET_DATE, help="目标日期 (默认 2026-07-29)")
    ap.add_argument("--apply", action="store_true", help="真正执行 (默认 dry-run)")
    ap.add_argument("--evidence", default="logs/repair_evidence_dl35.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        f"SELECT code, close, pre_close, "
        "ROUND((close-pre_close)/pre_close*100, 2) AS pct "
        f"FROM {TABLE} WHERE date=? AND change_pct=0 AND pre_close>0",
        (args.date,)).fetchall()
    n_total = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE date=?", (args.date,)).fetchone()[0]

    # 按板块涨跌幅限制判定越界 (坏 close), 排除不重算
    valid_codes, outliers = [], []
    for code, close, pre_close, pct in rows:
        if abs(pct) > board_limit(code) + 0.5:
            outliers.append({"code": code, "close": close,
                             "pre_close": pre_close, "recomputed_pct": pct,
                             "board_limit": board_limit(code)})
        else:
            valid_codes.append(code)

    print(f"[{'APPLY' if args.apply else 'dry-run'}] {args.db} date={args.date}")
    print(f"  当日总行 {n_total}, change_pct=0 且 pre_close>0 可重算 {len(rows)}")
    print(f"  按板块限制排除坏 close 行 {len(outliers)} (移交 DL-29 标记异常):")
    for o in outliers:
        print(f"    {o['code']}: 重算 {o['recomputed_pct']}% 超板块限制 "
              f"{o['board_limit']}% (close={o['close']} pre_close={o['pre_close']})")
    print(f"  实际重算行数 {len(valid_codes)}")

    if not args.apply:
        print("  (dry-run 不写库; 加 --apply 并确保已备份后才执行)")
        conn.close()
        return 0

    # 只重算板块限制内的行 (排除坏 close)
    placeholders = ",".join("?" * len(valid_codes))
    conn.execute(
        f"UPDATE {TABLE} SET change_pct = ROUND((close-pre_close)/pre_close*100, 2) "
        f"WHERE date=? AND change_pct=0 AND pre_close>0 AND code IN ({placeholders})",
        [args.date] + valid_codes)
    conn.commit()

    n_left = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE date=? AND change_pct=0",
        (args.date,)).fetchone()[0]
    print(f"  重算完成, 剩余 change_pct=0 行 {n_left} (应=排除的坏 close 行数)")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "card": "DL-35",
        "task": "fix_changepct_zero_batch",
        "db": args.db,
        "date": args.date,
        "rows_recomputed": len(valid_codes),
        "rows_excluded_bad_close": len(outliers),
        "excluded": outliers,
        "rows_left_zero": n_left,
        "note": "排除行 close 疑似串入次日值, 移交 DL-29 标记异常",
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
