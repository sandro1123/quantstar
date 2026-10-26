# -*- coding: utf-8 -*-
"""AUD-005 (P0) 存量处置 — ④ 22 只解冻 + ⑤ 18 笔伪造账回填。

背景 (审计回执 20260914_opus5_audit_P0_ALERT_settlement.md):
  代码面三重修法已并 (@0db1b3bd): 守卫口径同源 / 禁回落 entry_price / 顺延必告警。
  本脚本只处理**存量数据**: 已落库的 18 笔伪造账, 与卡死的 22 只 exit_pending。

纪律 (照 D1-b 清创卡先例):
  - 计划计算 (纯读) 与写入 (--apply) 分离; 默认 dry, dry 时以 mode=ro 打开库
  - dry 出全量 before/after 对拍账, 人核对后才允许真写
  - 必须显式传 --db (worktree 影子库 env-trap 已犯多次)
  - 幂等: 只认伪造行 (total_return_pct=0.0) 与 exit_pending, 重跑零副作用
  - 真写面归窗官执行; 本脚本作者不执行 --apply

代理口径声明 (不隐藏):
  存量 22 只的 trigger_date **全为 NULL** (入口绕过 mark_exit_trigger, 已由
  AUD-026 @4637c7e0 根治, 但只对**新**冻仓位生效)。故解冻计划对这些行
  **用 date(updated_at) 当触发日代理**, 并在每行标 date_source=updated_at_proxy,
  供窗官与用户按行判断是否接受该代理。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

_FAB_WHERE = ("status='exited' AND total_return_pct = 0.0 "
              "AND exit_reason LIKE 'T+1%'")


# --------------------------------------------------------------------------
# 公用
# --------------------------------------------------------------------------
def _trading_days(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_klines ORDER BY date")]


def _next_trading_day(days: list[str], after: str) -> str | None:
    for d in days:
        if d > after:
            return d
    return None


def _close(conn, code: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM daily_klines WHERE code=? AND date=?", (code, date)
    ).fetchone()
    return row[0] if row and row[0] and row[0] > 0 else None


def _fees(price: float, qty: int) -> dict:
    """卖出侧费用; constraint_checker 不可用时退化为零费 (只影响净收益列)。"""
    try:
        from execution_layer.core.constraint_checker import calculate_fees
        return calculate_fees(price, qty, is_buy=False)
    except Exception:
        return {"total": 0.0, "net_amount": price * qty}


def _settlement_math(entry_price: float, qty: int, close: float) -> dict:
    ret = (close - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    fees = _fees(close, qty) if qty > 0 else {"total": 0.0, "net_amount": close * qty}
    entry_cost = qty * entry_price
    net = fees["net_amount"] - entry_cost
    return {
        "new_exit_price": close,
        "new_return_pct": round(ret, 2),
        "new_total_fees": round(fees["total"], 2),
        "new_net_return": round(net, 2),
        "new_net_return_pct": round(net / entry_cost * 100, 2) if entry_cost > 0 else 0.0,
    }


# --------------------------------------------------------------------------
# ⑤ 回填: 18 笔伪造账
# --------------------------------------------------------------------------
def compute_backfill_plan(conn) -> list[dict]:
    """纯读: 对每笔伪造账按其 exit_date 的真收盘价重算。

    无该日 K 线的行标 resolvable=False 显形 —— 不得静默漏掉, 也不得回落任何价。
    """
    plan: list[dict] = []
    rows = conn.execute(
        "SELECT code, entry_price, position_qty, exit_price, exit_date, "
        "total_return_pct FROM stock_pool WHERE " + _FAB_WHERE + " ORDER BY exit_date, code"
    ).fetchall()
    for code, entry_price, qty, old_exit, exit_date, old_ret in rows:
        entry_price = entry_price or 0.0
        qty = qty or 0
        close = _close(conn, code, exit_date) if exit_date else None
        item = {
            "code": code, "exit_date": exit_date,
            "entry_price": entry_price, "qty": qty,
            "old_exit_price": old_exit, "old_return_pct": old_ret,
            "resolvable": close is not None,
            "new_exit_price": None, "new_return_pct": None,
            "new_total_fees": None, "new_net_return": None, "new_net_return_pct": None,
        }
        if close is not None:
            item.update(_settlement_math(entry_price, qty, close))
        plan.append(item)
    return plan


def apply_backfill_plan(conn, plan: list[dict]) -> int:
    n = 0
    for it in plan:
        if not it["resolvable"]:
            continue
        conn.execute(
            "UPDATE stock_pool SET exit_price=?, total_return_pct=?, total_fees=?, "
            "net_return=?, net_return_pct=?, "
            "exit_reason_detail=COALESCE(exit_reason_detail,'') || ' (P0 AUD-005 伪造账回填, 原值 0.00%)', "
            "updated_at=datetime('now','+8 hours') "
            "WHERE code=? AND " + _FAB_WHERE,
            (it["new_exit_price"], it["new_return_pct"], it["new_total_fees"],
             it["new_net_return"], it["new_net_return_pct"], it["code"]))
        n += conn.total_changes and 1 or 0
    return n


# --------------------------------------------------------------------------
# ④ 解冻: 22 只 exit_pending
# --------------------------------------------------------------------------
def compute_unfreeze_plan(conn) -> list[dict]:
    """纯读: 按各自触发日的 T+1 交易日真收盘价回溯结算。

    action 三态: settle (可回溯结算) / hold (T+1 尚未发生, 交给管道) /
    unresolvable (T+1 已到但该股无 K 线 —— 不回落任何价, 显形待查)。
    """
    days = _trading_days(conn)
    plan: list[dict] = []
    rows = conn.execute(
        "SELECT code, entry_price, position_qty, trigger_date, "
        "substr(updated_at,1,10) AS upd FROM stock_pool "
        "WHERE status='exit_pending' ORDER BY upd, code"
    ).fetchall()
    for code, entry_price, qty, trig, upd in rows:
        entry_price = entry_price or 0.0
        qty = qty or 0
        if trig:
            trigger_date, source = trig[:10], "trigger_date"
        else:
            trigger_date, source = (upd or ""), "updated_at_proxy"
        item = {
            "code": code, "entry_price": entry_price, "qty": qty,
            "trigger_date": trigger_date, "date_source": source,
            "settle_date": None, "settle_close": None,
            "new_return_pct": None, "new_total_fees": None,
            "new_net_return": None, "new_net_return_pct": None,
            "action": "unresolvable", "note": "",
        }
        t1 = _next_trading_day(days, trigger_date) if trigger_date else None
        if t1 is None:
            item["action"] = "hold"
            item["note"] = "触发日的 T+1 交易日尚未发生 — 交给已修好的结算腿自然处理"
            plan.append(item)
            continue
        close = _close(conn, code, t1)
        if close is None:
            item["note"] = f"T+1={t1} 已到但该股无当日 K 线 — 不回落任何价, 待查"
            plan.append(item)
            continue
        item["settle_date"] = t1
        item["settle_close"] = close
        item["action"] = "settle"
        item.update(_settlement_math(entry_price, qty, close))
        item["new_exit_price"] = close
        if source == "updated_at_proxy":
            item["note"] = "触发日为 updated_at 代理 (存量行 trigger_date 为 NULL)"
        plan.append(item)
    return plan


def apply_unfreeze_plan(conn, plan: list[dict]) -> int:
    n = 0
    for it in plan:
        if it["action"] != "settle":
            continue
        detail = ("daily_update T+1 回溯结算 (P0 AUD-005 冻结补结, 触发日="
                  f"{it['trigger_date']}/{it['date_source']})")
        conn.execute(
            "UPDATE stock_pool SET status='exited', exit_price=?, exit_date=?, "
            "exit_reason=COALESCE(exit_reason,'T+1确认卖出'), exit_reason_detail=?, "
            "total_return_pct=?, total_fees=?, net_return=?, net_return_pct=?, "
            "exit_low=?, last_price=?, last_price_date=?, "
            "updated_at=datetime('now','+8 hours') "
            "WHERE code=? AND status='exit_pending'",
            (it["settle_close"], it["settle_date"], detail,
             it["new_return_pct"], it["new_total_fees"], it["new_net_return"],
             it["new_net_return_pct"], it["settle_close"], it["settle_close"],
             it["settle_date"], it["code"]))
        n += 1
    return n


# --------------------------------------------------------------------------
# 下游污染面: post_exit_tracking 基线 + execution_stats 重算
# --------------------------------------------------------------------------
def compute_tracking_fix_plan(conn, backfill_plan):
    """post_exit_tracking 的 T+5/T+10/T+20 跟踪是**以结算时的 exit_price 为基线**创建的。
    那 18 笔结算时用的是伪造价, 故 54 行跟踪基线全错 —— 只回填主表等于只修一半。
    """
    fix = []
    for it in backfill_plan:
        if not it["resolvable"]:
            continue
        rows = conn.execute(
            "SELECT rowid, check_point, exit_price, total_return_pct "
            "FROM post_exit_tracking WHERE code=? AND exit_date=?",
            (it["code"], it["exit_date"])).fetchall()
        for rid, cp, old_px, old_ret in rows:
            fix.append({
                "rowid": rid, "code": it["code"], "check_point": cp,
                "old_exit_price": old_px, "old_return_pct": old_ret,
                "new_exit_price": it["new_exit_price"],
                "new_return_pct": it["new_return_pct"],
            })
    return fix


def apply_tracking_fix_plan(conn, fix):
    for f in fix:
        conn.execute(
            "UPDATE post_exit_tracking SET exit_price=?, total_return_pct=? WHERE rowid=?",
            (f["new_exit_price"], f["new_return_pct"], f["rowid"]))
    return len(fix)


def affected_stats_dates(backfill_plan, unfreeze_plan):
    """execution_stats 的 total_trades/win_count/loss_count 按 exit_date 统计 ——
    回填改了收益、解冻新增了了结日, 两边都会改动当日胜负计数, 故都要重算。
    hold 的不计入 (没产生 exited 行)。
    """
    days = {it["exit_date"] for it in backfill_plan
            if it.get("resolvable") and it.get("exit_date")}
    days |= {it["settle_date"] for it in unfreeze_plan
             if it.get("action") == "settle" and it.get("settle_date")}
    return sorted(days)


def recompute_execution_stats(db_path, dates):
    """用**已修好的** save_daily 重算 —— 分组计数病已于 716c6281 修掉, 故重算会
    同时修正 correct/neutral/wrong_exits 与 exit_accuracy; save_daily 自带同日
    先删后插的幂等。"""
    from common.database import Database
    from execution_layer.stats.performance import ExecutionStats

    stats = ExecutionStats(Database(db_path))
    for day in dates:
        stats.save_daily(day)
    return len(dates)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_backfill(plan: list[dict]) -> None:
    print("\n=== ⑤ 回填计划 (18 笔伪造账) ===")
    print(f"{'code':<8} {'exit_date':<12} {'entry':>8} {'旧exit':>8} {'旧ret':>7} "
          f"{'新exit':>8} {'新ret':>8}  可解")
    tot_old = tot_new = 0.0
    for it in plan:
        print(f"{it['code']:<8} {str(it['exit_date']):<12} {it['entry_price']:>8.2f} "
              f"{(it['old_exit_price'] or 0):>8.2f} {(it['old_return_pct'] or 0):>7.2f} "
              f"{(it['new_exit_price'] or 0):>8.2f} {(it['new_return_pct'] or 0):>+8.2f}  "
              f"{'Y' if it['resolvable'] else '*** N ***'}")
        tot_old += it["old_return_pct"] or 0.0
        tot_new += it["new_return_pct"] or 0.0
    bad = [i["code"] for i in plan if not i["resolvable"]]
    print(f"小计 {len(plan)} 笔: 旧收益合计 {tot_old:+.2f}% → 真值合计 {tot_new:+.2f}%")
    if bad:
        print(f"*** 不可解 {len(bad)} 笔 (无当日 K 线, 不会被写): {bad}")


def _print_unfreeze(plan: list[dict]) -> None:
    print("\n=== ④ 解冻计划 (exit_pending) ===")
    print(f"{'code':<8} {'触发日':<12} {'依据':<18} {'T+1':<12} {'收盘':>8} {'真收益':>9}  动作")
    for it in plan:
        print(f"{it['code']:<8} {it['trigger_date']:<12} {it['date_source']:<18} "
              f"{str(it['settle_date'] or '-'):<12} "
              f"{(it['settle_close'] or 0):>8.2f} "
              f"{(it['new_return_pct'] or 0):>+9.2f}  {it['action']}"
              + (f"  [{it['note']}]" if it["note"] else ""))
    from collections import Counter
    c = Counter(i["action"] for i in plan)
    proxy = sum(1 for i in plan if i["date_source"] == "updated_at_proxy")
    print(f"小计 {len(plan)} 只: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print(f"*** 其中 {proxy} 只用 updated_at 代理触发日 (存量 trigger_date 为 NULL, "
          f"AUD-026 已根治新增面) —— 请按行确认是否接受该代理口径")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AUD-005 存量处置 (默认 dry)")
    ap.add_argument("--db", required=True, help="生产库绝对路径 (必须显式传, 防影子库)")
    ap.add_argument("--what", choices=["backfill", "unfreeze", "both"], default="both")
    ap.add_argument("--apply", action="store_true", help="真写 (默认只出 dry 账)")
    a = ap.parse_args(argv)

    if a.apply:
        conn = sqlite3.connect(a.db)
    else:
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)  # dry 只读, 物理防误写

    bf = uf = []
    if a.what in ("backfill", "both"):
        bf = compute_backfill_plan(conn)
        _print_backfill(bf)
    if a.what in ("unfreeze", "both"):
        uf = compute_unfreeze_plan(conn)
        _print_unfreeze(uf)

    if not a.apply:
        tf = compute_tracking_fix_plan(conn, bf) if bf else []
        days = affected_stats_dates(bf, uf)
        print()
        print("=== 下游连带 (apply 时自动执行) ===")
        print(f"post_exit_tracking 基线重算: {len(tf)} 行 "
              f"(以伪造 exit_price 为基线创建的 T+5/T+10/T+20 跟踪)")
        print(f"execution_stats 重算: {len(days)} 日 {days} "
              f"(胜负计数由上两表派生; save_daily 分组计数病已于 716c6281 修掉)")
        print()
        print("[DRY] 未写任何数据。真写需 --apply, 且按纪律由窗官执行。")
        return 0

    tf = compute_tracking_fix_plan(conn, bf) if bf else []
    n1 = apply_backfill_plan(conn, bf) if bf else 0
    n3 = apply_tracking_fix_plan(conn, tf) if tf else 0
    n2 = apply_unfreeze_plan(conn, uf) if uf else 0
    conn.commit()
    conn.close()
    days = affected_stats_dates(bf, uf)
    n4 = recompute_execution_stats(a.db, days) if days else 0
    print()
    print(f"[APPLY] 回填 {n1} 笔 / post_exit_tracking 基线重算 {n3} 行 / "
          f"解冻结算 {n2} 只 / execution_stats 重算 {n4} 日 {days}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
