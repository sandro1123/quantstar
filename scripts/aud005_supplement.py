# -*- coding: utf-8 -*-
"""AUD-005 补充处置 — 状态流转甩出作用域的伪造账（2 只）。

为什么另起一个脚本而不改 aud005_remediation.py:
  那个脚本已经过统筹验收并执行过 apply, 改它等于让已验收件失效。补充件独立。

病根 (本次 apply 后复验发现):
  主脚本作用域按**状态**圈 (`status='exited' AND exit_reason LIKE 'T+1%'`),
  而状态是会流转的 —— 300906/600152 被伪造结算后因"未建仓收到 sell_signal"
  被打回 watch, **exit_* 字段却没清**, 于是双双落在作用域外。
  伪造的特征是**值形态**(卖出价恰等于入场价 + 收益恰为 0), 不是状态。
  本脚本按值形态圈, status/exit_reason 一律不看。

⚠️ 了结日冲突 (须人判, 脚本不自作主张):
  300906 的 stock_pool.exit_date 已被 expire_stale_watch 二次退出**覆盖**成
  2026-09-14(今日), 而 post_exit_tracking 保留着原始的 2026-09-07。
  同一笔了结两张表两个故事。按 stock_pool 的日期查 K 线会查到今天(未收盘)=错。
  本脚本: 两个日期都打出来, 冲突行显式标 date_conflict, 并用 tracking 日(原始未被
  覆盖的那个)算真值 —— 但**标出来给人看**, 不埋在结果里。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

# 值形态作用域: 卖出价恰等于入场价 + 收益恰为 0 + 未回填过(幂等锚)
_WHERE = (
    "total_return_pct = 0.0 AND exit_price IS NOT NULL "
    "AND entry_price IS NOT NULL AND exit_price = entry_price "
    "AND (exit_reason_detail IS NULL "
    "     OR exit_reason_detail NOT LIKE '%AUD-005 伪造账回填%')"
)


def resolve_exit_date(pool_date, tracking_date):
    """了结日择定 + 冲突标记。

    tracking 日是**首次结算时写的、未被后续状态流转覆盖**的原始值;
    pool 日可能已被二次退出 (watch_expired 等) 覆盖。冲突时以 tracking 为准,
    但必须把冲突显形 —— 这是人该看见的事实, 不是脚本该默默吞掉的。
    """
    if tracking_date and pool_date and tracking_date != pool_date:
        return tracking_date, True
    return (tracking_date or pool_date), False


def compute_supplement_plan(conn) -> list[dict]:
    plan = []
    rows = conn.execute(
        "SELECT code, status, entry_price, position_qty, exit_price, exit_date, "
        "exit_reason, total_return_pct FROM stock_pool WHERE " + _WHERE
        + " ORDER BY code").fetchall()
    for code, status, entry, qty, old_exit, pool_date, reason, old_ret in rows:
        t = conn.execute(
            "SELECT DISTINCT exit_date FROM post_exit_tracking WHERE code=? "
            "ORDER BY exit_date LIMIT 1", (code,)).fetchone()
        tracking_date = t[0] if t else None
        use_date, conflict = resolve_exit_date(pool_date, tracking_date)
        close = None
        if use_date:
            k = conn.execute(
                "SELECT close FROM daily_klines WHERE code=? AND date=?",
                (code, use_date)).fetchone()
            close = k[0] if k and k[0] and k[0] > 0 else None
        item = {
            "code": code, "status": status, "entry_price": entry, "qty": qty or 0,
            "pool_exit_date": pool_date, "tracking_exit_date": tracking_date,
            "use_date": use_date, "date_conflict": conflict,
            "exit_reason": reason, "old_return_pct": old_ret,
            "new_exit_price": close, "new_return_pct": None,
            "resolvable": close is not None,
        }
        if close is not None and entry:
            item["new_return_pct"] = round((close - entry) / entry * 100, 2)
        plan.append(item)
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AUD-005 补充处置 (只出 dry 账)")
    ap.add_argument("--db", required=True, help="生产库绝对路径 (必须显式传)")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)   # 只读, 本件不提供 --apply

    plan = compute_supplement_plan(conn)
    print("=== AUD-005 补充 dry 账 (值形态作用域, 不看 status/exit_reason) ===")
    if not plan:
        print("无命中 —— 值形态作用域下已无未回填的伪造账。")
        return 0
    print(f"{'code':<8} {'status':<14} {'pool日':<12} {'tracking日':<12} "
          f"{'采用':<12} {'入场':>8} {'真close':>8} {'真收益':>9}  冲突")
    for it in plan:
        print(f"{it['code']:<8} {str(it['status']):<14} "
              f"{str(it['pool_exit_date']):<12} {str(it['tracking_exit_date']):<12} "
              f"{str(it['use_date']):<12} {it['entry_price']:>8.2f} "
              f"{(it['new_exit_price'] or 0):>8.2f} "
              f"{(it['new_return_pct'] or 0):>+9.2f}  "
              f"{'⚠️ 是' if it['date_conflict'] else '否'}"
              + ("" if it["resolvable"] else "   *** 无K线不可解 ***"))
    n_conf = sum(1 for i in plan if i["date_conflict"])
    print(f"\n小计 {len(plan)} 只, 其中 {n_conf} 只**了结日两表冲突**")
    if n_conf:
        print("⚠️ 冲突行采用 tracking 日 (首次结算写入、未被后续状态流转覆盖的原始值);")
        print("   stock_pool 的日期已被二次退出 (如 watch_expired) 覆盖 —— 请人工确认后再 apply。")
    print("\n[DRY] 本脚本不提供 --apply。真写请统筹据此账另行执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
