# -*- coding: utf-8 -*-
"""EX-4 CLI 接线 — intraday_scan 收盘轮 advisor 闭环段 + 扫描域收窄对比账。

派单 sm-dbdcf773 两半件:
  ① advisor_segment: 收盘轮 (≥15:00) 扫描后段 — pool_maintenance (推荐池到期降级
     + 跟踪池三规则退出) + 三态清单留痕 (action_records 'advisory_state' 每候选
     一行, buy_now/wait_watch/pass 供报告/复盘面)。
  ② domain_account: 扫描域收窄对比账 — full (全池状态分布) vs narrowed
     (pending+watch+active = advisor.scan_codes 单一口径) — 接线实录可验。

边界 (协同红线): monitor.py 零触碰 (三序契约面 + B 席护栏落点 278/286 属 B 职权);
本模块全落 scripts/ 层, 由 intraday_scan CLI 在 el.intraday_scan() 后调用
(cron 直驱 — 收盘轮 15:00 后最后一跑自然携带, 盘中轮 now<15:00 静默跳过)。

用法: PYTHONPATH=src python -m execution_layer.scripts.ex4_cli_scan [--date D] [--now HH:MM]
"""
from __future__ import annotations

import argparse
from datetime import datetime

from common.logging import get_logger

logger = get_logger("execution.ex4_cli_scan")

CLOSE_CUTOFF = "15:00"   # 收盘轮判线: 盘中不跑池维护 (防盘中状态漂移)


def _now_hhmm(default: str = "") -> str:
    if default:
        return default
    return datetime.now().strftime("%H:%M")


def domain_account(db) -> dict:
    """② 扫描域收窄对比账 — full vs narrowed (scan_codes 单一口径)。"""
    from execution_layer.advisor.recommendation_advisor import scan_codes
    conn = db.get_connection()
    by_status: dict[str, int] = {}
    for r in conn.execute(
            "SELECT status, COUNT(*) FROM stock_pool GROUP BY status").fetchall():
        by_status[r[0]] = r[1]
    narrowed_codes = scan_codes(db)
    full = sum(by_status.values())
    return {"full": full, "narrowed": len(narrowed_codes),
            "excluded": full - len(narrowed_codes), "by_status": by_status}


def advisor_segment(db, today: str = "", now_hhmm: str = "") -> int:
    """① 收盘轮 advisor 段 — 池维护 + 三态留痕。返回状态迁移+留痕行数。

    盘中轮 (now < 15:00) 静默跳过 (返回 0): 池状态迁移属日终动作,
    盘中跑会造成 watch/exited 状态漂移, 与 intraday 盘中检查互相打架。
    """
    if _now_hhmm(now_hhmm) < CLOSE_CUTOFF:
        return 0
    from execution_layer.advisor.recommendation_advisor import (
        pool_maintenance)
    conn = db.get_connection()
    t = (today or datetime.now().strftime("%Y-%m-%d"))[:10]

    moved = pool_maintenance(db, today=t)

    # 三态清单留痕 (advisory_state 每候选一行 — 周报/复盘面可见)
    rows = conn.execute(
        "SELECT code, status, last_price FROM stock_pool "
        "WHERE status IN ('pending_entry', 'watch')").fetchall()
    # AG-DEBATE-V2 三段: 桥版通电 — 失效条件警示面 (跨腿消费)。
    # 16:00 辩论腿由 _run_debate 出口落 agent_invalidation_warnings;
    # 本收盘轮按 (code, 当日) 查表装配伪 ExpertReport 喂桥
    # (桥只读 expert_name + invalidation_conditions 字段面)。
    # 三态判定零改动 (桥内部走原版 advise); 无警示行 → 桥零附加
    # → reason 与原版逐字等价 (零回归锚)。
    from agent_layer.advisor.invalidation_bridge import (
        advise_with_invalidation)
    from types import SimpleNamespace
    t_day = t[:10]
    ledger = 0
    for r in rows:
        # 查表容错: 表缺 (首日 15:00 早于 16:00 辩论建表) → 无警示
        # → 桥零附加 → reason 原版等价 (零回归锚, 铁律 #12 不静默 —
        # 表缺是时序常态非异常, 不告警)
        try:
            warns = conn.execute(
                "SELECT expert_name, condition FROM agent_invalidation_warnings "
                "WHERE code=? AND date=?", (r[0], t_day)).fetchall()
        except Exception:
            warns = []
        reports = [SimpleNamespace(
            expert_name=w[0], invalidation_conditions=[w[1]])
            for w in warns] if warns else None
        out = advise_with_invalidation(
            db, r[0], last_price=float(r[2] or 0), today=t,
            expert_reports=reports)
        reason = f"{out['state']}: {out.get('reason', '')[:120]}"
        # 警示摘要面: 跨专家去重条目 (桥已去重截 8), 每条 [专家:条件] 摘要
        inv = out.get("invalidation_warnings") or []
        if inv:
            digest = "; ".join(
                f"[{w['source_expert']}] {w['condition']}" for w in inv[:3])
            reason = f"{reason} | 失效警示: {digest}"[:280]
        conn.execute(
            "INSERT INTO action_records (code, action_type, reason, source) "
            "VALUES (?, 'advisory_state', ?, 'execution')",
            (r[0], reason))
        ledger += 1
    conn.commit()
    logger.info(
        f"ex4_cli_scan advisor 段: 迁移 {moved} + 三态留痕 {ledger} 行 (date={t})")
    return moved + ledger


def run(db, today: str = "", now_hhmm: str = "") -> int:
    """CLI 编排: 域账 (常跑) + advisor 段 (仅收盘轮, run 层判线)。rc=0 常态。"""
    acct = domain_account(db)
    logger.info(
        f"ex4_cli_scan 域账: full={acct['full']} narrowed={acct['narrowed']} "
        f"excluded={acct['excluded']} by_status={acct['by_status']}")
    if _now_hhmm(now_hhmm) >= CLOSE_CUTOFF:
        advisor_segment(db, today=today, now_hhmm=now_hhmm)
    else:
        logger.info("ex4_cli_scan: 盘中轮, advisor 段跳过 (收盘后 15:00 才跑池维护)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None)
    parser.add_argument("--now", default=None, help="HH:MM (缺省=当前时刻)")
    args = parser.parse_args()

    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env"))
    from common.database import Database
    db = Database()
    return run(db, today=args.date or "", now_hhmm=args.now or "")


if __name__ == "__main__":
    import sys
    sys.exit(main())
