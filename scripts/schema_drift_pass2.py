# -*- coding: utf-8 -*-
"""件1 第二轮 · 把第一版的两处「线索」收窄成「结论」（只读）。

第一版报了两条 P1（代码独有 CHECK）与「仅生产有 48 张表」。两条都**不能直接当结论**：

① **「代码独有 CHECK」不等于「回灌会被拒」** —— 判据漏了一步:
   要看**生产数据是否真的违反**那条 CHECK。有约束而数据全满足 = 无害。
   反例就在眼前: `action_records` 那条白名单是我自己 AUD-028 扩到 33 项的,
   扩完很可能已覆盖生产所有取值 —— 若如此, 它是**已修好的**, 不是 P1。
   **把"有约束"当成"会拒数据"报出去, 就是我一路在治的「判据面 ≠ 事实面」。**

② **「仅生产有 48 张」只说明"无模块级 DDL 常量"** —— 第一版的收集器只扫模块级
   字符串常量, **函数体内的 `CREATE TABLE` 它看不见**。
   **别把「我的工具看不到」说成「它不存在」**（本人判例 J-R2-11, 昨天刚犯过一次）。

本轮两件事: 实打 CHECK / 全树找建表点。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

_CHECK = re.compile(r"CHECK\s*\((.*?)\)\s*(?:,|\))", re.S | re.I)
# 函数体内也算：任何出现 CREATE TABLE <name> 的源码位置
_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)", re.I)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="件1 第二轮收窄 (只读)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--src", required=True)
    a = ap.parse_args(argv)
    src = Path(a.src)
    prod = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)

    tabs = sorted(r[0] for r in prod.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    print("=" * 74)
    print("件1 第二轮 · 收窄")
    print(f"  生产库 {a.db}  ({len(tabs)} 表) ｜ 代码树 {src}")
    print("=" * 74)

    # ---------------- ① 代码独有 CHECK: 生产数据到底违不违反
    print("\n--- ① 代码独有 CHECK 的**实打**: 生产现有数据是否违反 ---")
    print("    (判据: 把该 CHECK 加到临时表上, 逐行灌生产数据, 数被拒的行)")
    CASES = [
        ("action_records", "action_type",
         "action_type IN ('add_to_watch','remove_from_watch','set_entry_range',"
         "'cancel_entry','cancel_exit','entry','skip_entry','add_position',"
         "'reduce_position','swap_exit','partial_exit','exit','skip_exit',"
         "'exit_check','exit_tracking','exit_tracking_check','status_change',"
         "'hold','sell_signal','adjust_stop_loss','adjust_take_profit',"
         "'adjust_trailing_stop','adjust_entry_range','pnl_change',"
         "'manual_override','volatility_alert','advisory_state','pool_downgrade',"
         "'pool_exit','buy_pending_review','sell_pending_review','buy_rejected',"
         "'stale_pending_cleanup')"),
        ("action_records", "source",
         "source IN ('strategy','agent','execution','manual')"),
        ("stock_pool", "exit_source",
         "exit_source IN ('cleanup','engine','manual') OR exit_source IS NULL"),
        ("stock_pool", "status",
         "status IN ('watch','pending_entry','active','exit_pending','exited')"),
        ("stock_pool", "source",
         "source IN ('strategy','manual','agent') OR source IS NULL"),
    ]
    verdicts = []
    for table, col, expr in CASES:
        try:
            total = prod.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            bad = prod.execute(
                f"SELECT COUNT(*) FROM {table} WHERE NOT ({expr})").fetchone()[0]
            distinct_bad = [r[0] for r in prod.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE NOT ({expr}) LIMIT 8")]
        except sqlite3.Error as e:
            print(f"  [探针错, 非发现] {table}.{col}: {str(e)[:60]}")
            continue
        ok = bad == 0
        verdicts.append((table, col, ok, bad, total))
        mark = "回灌通过" if ok else f"**{bad} 行会被拒**"
        print(f"  {table}.{col:<14} 生产 {total:>5} 行 -> {mark}"
              + (f"   越界值 {distinct_bad}" if not ok else ""))

    # ---------------- ② 那 48 张表: 代码里到底有没有建表路径
    print("\n--- ② 「仅生产有」的表: 全树找建表点 (含函数体内) ---")
    created: dict[str, list[str]] = {}
    for p in sorted(src.rglob("*.py")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _CREATE.findall(text):
            created.setdefault(m, []).append(str(p.relative_to(src)))
    # 也扫 scripts/（恢复/回补类脚本常在这儿建表）
    scripts = src.parent / "scripts"
    script_created: dict[str, list[str]] = {}
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*.py")):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in _CREATE.findall(text):
                script_created.setdefault(m, []).append(str(p.relative_to(scripts.parent)))

    no_path, src_only, script_only = [], [], []
    for t in tabs:
        in_src, in_scr = t in created, t in script_created
        if in_src:
            src_only.append(t)
        elif in_scr:
            script_only.append(t)
        else:
            no_path.append(t)
    print(f"  src/ 内有建表语句      : {len(src_only)} 张")
    print(f"  仅 scripts/ 内有       : {len(script_only)} 张  {script_only[:10]}")
    print(f"  **全树找不到建表语句** : {len(no_path)} 张")
    for t in no_path:
        try:
            n = prod.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            n = -1
        print(f"      {t:<38} {n:>9} 行")

    print("\n" + "=" * 74)
    p1 = [v for v in verdicts if not v[2]]
    print(f"① CHECK 实打: {len(verdicts)} 条受检, **真会拒数据的 {len(p1)} 条**"
          + (f" -> {[(v[0], v[1], v[3]) for v in p1]}" if p1 else " (全部通过)"))
    print(f"② 建表路径: src {len(src_only)} / 仅 scripts {len(script_only)} / "
          f"**无路径 {len(no_path)}**")
    print("=" * 74)
    prod.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
