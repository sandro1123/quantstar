# -*- coding: utf-8 -*-
"""AUD-027 统计漏账处置 — dry 账（统筹 sm-f8184cb7 裁 GO，写生产库归统筹）。

本脚本只读（`mode=ro`），**不提供 `--apply`**。

## 病灶
`ExecutionStats.save_daily` 的作用域是 `status='exited' AND exit_date=?`：认的是
**当前状态**。而 `stock_pool` 是 code 主键单行表，「当前状态」与「历史了结账」挤在
同一行 —— 票了结后被重新加回观察池，那笔真实交易就跟着行流转走，掉出统计面。

## 一处自我更正（比病灶本身重要）
我此前向统筹表述为「直接清 `exit_*` 会让那笔交易**彻底消失**」——**说过头了**。
取证后为准：`post_exit_tracking` 是一张 **210 行的了结台账**，600152 那笔的
`exit_date` / `entry_price` / `exit_price` / `exit_reason` 在里面**完整存着**。
所以清 `stock_pool.exit_*` **不会丢账**。真正的缺口是：
**`execution_stats` 只读 `stock_pool`、且按 status 圈**，于是统计面少这一笔。
把「统计面看不见」说成「账没了」，会把人推向错误的处置顺序 —— 这正是我一路在治的
「判据面 ≠ 事实面」，这次我自己犯在表述上。

## 补账为什么不耐久（这条决定处置路线）
`save_daily` 是幂等重跑设计：同日先 `DELETE FROM execution_stats WHERE period='daily'
AND period_start=?` 再 `INSERT`。**任何手工补进 execution_stats 的行，下一次为该日
重跑 save_daily 时会被整行冲掉。** 所以「补账」在口径不变的前提下只是一次性涂改，
不是修复。

## 三条路（不替人选，把代价摆出来）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

_ORPHAN = ("SELECT code, status, exit_date, exit_reason, entry_price, exit_price, "
           "total_return_pct FROM stock_pool "
           "WHERE exit_date IS NOT NULL AND status <> 'exited' "
           "ORDER BY exit_date, code")

_EXIT_FIELDS = (
    "exit_reason", "exit_reason_detail", "exit_layer", "exit_date", "exit_source",
    "exit_price", "exit_low", "exit_high", "exit_plan_date",
    "trigger_price", "trigger_date",
    "total_return", "total_return_pct", "net_return", "net_return_pct",
)


def survives_elsewhere(conn, code: str, exit_date: str) -> list[dict]:
    """这笔了结在 post_exit_tracking 里还有没有账 —— 决定清字段是否丢账。"""
    rows = conn.execute(
        "SELECT check_point, check_date, entry_price, exit_price, exit_reason "
        "FROM post_exit_tracking WHERE code=? AND exit_date=? ORDER BY id",
        (code, exit_date)).fetchall()
    return [dict(zip([d[0] for d in conn.execute(
        "SELECT check_point, check_date, entry_price, exit_price, exit_reason "
        "FROM post_exit_tracking LIMIT 0").description], r)) for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AUD-027 统计漏账 dry 账 (只读)")
    ap.add_argument("--db", required=True, help="生产库绝对路径 (须显式传)")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)

    orphans = conn.execute(_ORPHAN).fetchall()
    print("=== AUD-027 统计漏账 dry 账 (只读, 无 --apply) ===\n")
    if not orphans:
        print("零漏账 —— exit_date 非空的行全部仍在 exited 态。")
        return 0

    print(f"漏账 {len(orphans)} 笔（`exit_date` 非空但 `status != 'exited'`）:\n")
    print(f"{'code':<8}{'status':<14}{'了结日':<12}{'入场':>7}{'卖出':>7}{'真收益':>9}  了结原因")
    for code, status, ed, reason, ep, xp, ret in orphans:
        print(f"{code:<8}{status:<14}{ed:<12}{ep:>7.2f}{xp:>7.2f}{ret:>+9.2f}  {reason}")

    print("\n--- 逐笔处置面 ---")
    for code, status, ed, reason, ep, xp, ret in orphans:
        print(f"\n[{code}] 现状: status={status}, 了结账挂在活态行上")

        # ① 账是否已在别处 (决定清字段是否丢账)
        t = conn.execute(
            "SELECT check_point, check_date FROM post_exit_tracking "
            "WHERE code=? AND exit_date=? ORDER BY id", (code, ed)).fetchall()
        if t:
            print(f"  ① 了结台账: post_exit_tracking **有 {len(t)} 行**"
                  f" (检查点 {', '.join(x[0] or '?' for x in t)})")
            print(f"     -> 清 stock_pool.exit_* **不丢账**, 原始了结记录在 tracking 表")
        else:
            print(f"  ① 了结台账: post_exit_tracking **无此笔** "
                  f"-> 清字段前必须先落账, 否则真的丢")

        # ② 统计面缺口
        s = conn.execute(
            "SELECT total_trades, win_count, loss_count FROM execution_stats "
            "WHERE period='daily' AND period_start=?", (ed,)).fetchone()
        if s:
            w = 1 if (ret or 0) > 0 else 0
            print(f"  ② 统计面: {ed} 现为 total={s[0]} win={s[1]} loss={s[2]}; "
                  f"补这笔应为 total={s[0]+1} win={s[1]+w} loss={s[2]+1-w}")
        else:
            print(f"  ② 统计面: {ed} **无统计行**")
        print(f"     ⚠️ 手工补进 execution_stats **不耐久**: save_daily 为该日重跑时"
              f" DELETE+INSERT 会整行冲掉")

        # ③ 清字段会清掉什么
        row = conn.execute(
            f"SELECT {', '.join(_EXIT_FIELDS)} FROM stock_pool WHERE code=?",
            (code,)).fetchone()
        dirty = [f for f, v in zip(_EXIT_FIELDS, row) if v is not None]
        print(f"  ③ 清字段将置 NULL 的列 ({len(dirty)}): {dirty}")

    print("\n" + "=" * 72)
    print("三条路（代价摆出来，不替人选）:\n")
    print("甲 · 只清字段，接受统计面少这 N 笔")
    print("   代价: execution_stats 永久少账; 账本身在 post_exit_tracking 不丢")
    print("   适用: 认为「统计面 = 当前持仓口径的了结」而非「全部历史交易」")
    print("\n乙 · 先手工补 execution_stats，再清字段")
    print("   代价: **补账不耐久** —— save_daily 为该日重跑即冲掉, 且下次流转再犯")
    print("   适用: 只为一次性对外交卷; 不解决机制")
    print("\n丙 · 改 save_daily 口径（按了结**事件**而非**当前状态**圈），再清字段")
    print("   代价: 动既有统计语义, 需回算历史; 属产品裁定")
    print("   适用: 根治 —— 「按状态圈会漏, 按事件/值形态圈才稳」")
    print("\n作者倾向: **丙为根治、甲为当下可行**; 乙除非有对外交卷硬需求否则不建议")
    print("（丙涉及统计口径变更, 已呈裁, 本脚本不替人选。）")
    print("\n[DRY] 本脚本不提供 --apply。真写请统筹据此账另行执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
