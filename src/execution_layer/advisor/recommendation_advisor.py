# -*- coding: utf-8 -*-
"""EX-4 三态建议引擎 — RecommendationAdvisor (执行层旗舰件, 2026-09-09)。

用户愿景 (qs-execution-layer-user-vision): 推荐股全生命周期跟踪 + 操作建议管家,
盘中操作建议=重点, 少而精 N 只。派单 sm-4382c008。

四件:
  A. advise() 三态: buy_now (条件单四件套) / wait_watch (附条件+有效期) /
     pass (附原因) — 规则定骨架 (entry/exit engine 现成判据的镜像口径),
     LLM 人话面留 build_request 工厂对齐位 (B席工厂落地后接)。
  B. pool_maintenance() 池退出: 推荐池条件单到期未触发→降级回跟踪池;
     跟踪池三规则 (观察期低点 X% / 成交额萎缩 / 连续 N 日无信号) →
     exited + exit_reason 落表 (周报 exit_attribution 段可见)。
  C. promote_recommendations() 推荐位收口: N=clamp(3,8) 断层浮动;
     pool-first (跟踪池优先升推荐) + composite_score 补位 + risk 强负面
     (verdict∈{不推荐} 且 conf>0.7) 前置否决。
  D. intraday 域收窄: scan_codes() 推荐池+跟踪池优先 (monitor 消费,
     非全盘 — 性能顺手件)。

契约: 全部输出 dict (JSON 可序列化) — report_layer 四行结构呈现面。
EX-1 benchmark (2895751) 与 EX-3 快照 (276e261) 依赖按「09-11 起有数」
口径: 本模块零直连 (benchmark/快照表由 fx_settlement/settle_daily 写),
回放面 09-11 后自然有数。
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("execution.advisor")

# ---- 参数面 (B4: config 可调, DEFAULT 兜底) ----
DEFAULT_EXIT_CONFIG = {
    "obs_low_drop_pct": 8.0,     # 跟踪池跌破观察期低点 X%
    "no_signal_days": 10,        # 连续 N 日无信号
    "volume_shrink_ratio": 0.5,  # 成交额萎缩至入围一半以下
}
N_MIN, N_MAX = 3, 8             # 推荐位 clamp 边界
GAP_RATIO = 0.15                # 断层浮动: 候选相邻分差 > 15% 视为断层截断
RISK_VETO_CONF = 0.7            # risk 前置否决置信线
RISK_VETO_VERDICTS = ("不推荐",)  # AG-03 统一词表


def clamp_n(n: int) -> int:
    """C2: N=clamp(3,8)。"""
    return max(N_MIN, min(N_MAX, int(n)))


def _latest_risk(conn, code: str, today: str) -> tuple[str, float] | None:
    """最新 agent 预测 verdict+confidence (risk 否决源, 只看今日)。"""
    try:
        r = conn.execute(
            "SELECT verdict, confidence FROM agent_predictions "
            "WHERE code=? AND created_at >= ? "
            "ORDER BY id DESC LIMIT 1", (code, today[:10])).fetchone()
        if r and r[0]:
            return str(r[0]), float(r[1] or 0)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# A. 三态判定
# ---------------------------------------------------------------------------

def advise(db, code: str, last_price: float = 0.0, today: str = "",
           config: dict | None = None) -> dict:
    """逐票三态 — 规则骨架 (LLM 人话面 = build_request 工厂对齐位, 后接)。

    判序: ①risk 前置否决 → ②条件单过期 → ③价区间 → ④默认 wait_watch。
    """
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM stock_pool WHERE code=?", (code,)).fetchone()
    if row is None:
        return {"state": "pass", "reason": "不在池内"}
    row = dict(row) if not isinstance(row, dict) else row
    status = row.get("status")
    if status not in ("pending_entry", "watch"):
        return {"state": "pass", "reason": f"状态 {status} 非推荐/跟踪池"}

    t = (today or "")[:10]
    # ① risk 前置否决 (C3 同口径, 三态面前置)
    risk = _latest_risk(conn, code, t)
    if risk and risk[0] in RISK_VETO_VERDICTS and risk[1] > RISK_VETO_CONF:
        return {"state": "pass",
                "reason": f"risk 否决: 最新研判「{risk[0]}」(conf={risk[1]:.2f} > {RISK_VETO_CONF})"}

    el, eh = float(row.get("entry_low") or 0), float(row.get("entry_high") or 0)
    vu = str(row.get("valid_until") or "")[:10]
    # ② 条件单过期 (B1 降级判定同口径, 但 advise 只出态不改池 — 改池归 pool_maintenance)
    if vu and t and t > vu:
        return {"state": "pass", "reason": f"条件单已过期 (valid_until={vu}), 未触发作废"}

    # ③ 价区间判定 (entry engine 判据镜像: 区间内=可买)
    if el > 0 and eh > 0 and el <= eh:
        if last_price and el <= last_price <= eh:
            stop = round(el * 0.94, 2)      # 止损 = 区间下沿 -6%
            target = round(eh * 1.12, 2)    # 目标 = 区间上沿 +12%
            return {
                "state": "buy_now",
                "reason": f"现价 {last_price} 在建议价区 [{el}, {eh}] 内",
                "order_plan": {
                    "entry_low": el, "entry_high": eh,
                    "trigger": round((el + eh) / 2, 2),
                    "target": target, "stop_loss": stop,
                },
            }
        if last_price and last_price > eh:
            return {
                "state": "wait_watch",
                "reason": f"现价 {last_price} 高于价区上沿 {eh}",
                "condition": f"等回踩价区上沿 {eh} (或 5 日线企稳)",
                "valid_days": 5,
            }
        if last_price and last_price < el:
            return {
                "state": "wait_watch",
                "reason": f"现价 {last_price} 低于价区下沿 {el}",
                "condition": f"等放量站回价区下沿 {el} 之上",
                "valid_days": 5,
            }
    # ④ 无区间/无价 → wait_watch (证据不足不武断 pass)
    return {
        "state": "wait_watch",
        "reason": "无有效价区或现价, 证据待补",
        "condition": "待日终数据面更新后复判",
        "valid_days": 3,
    }


# ---------------------------------------------------------------------------
# B. 池退出 (三池一门: 推荐池到期降级 → 跟踪池三规则 → exited)
# ---------------------------------------------------------------------------

def pool_maintenance(db, today: str = "", config: dict | None = None) -> int:
    """日终池维护 — 推荐池到期降级 + 跟踪池退出三规则。返回状态迁移数。"""
    cfg = dict(DEFAULT_EXIT_CONFIG)
    cfg.update(config or {})
    conn = db.get_connection()
    t = (today or "")[:10]
    n_moved = 0

    # AUD-027: 绕行路径① —— 下面直写 status 不经 StockPool.update_status,
    # 只修正门会漏 (与 AUD-026「在册双入口、实测三入口」同构)。
    from execution_layer.core.stock_pool import exit_fields_null_sql
    null_sql = exit_fields_null_sql()
    # AUD-029: 回滚判据与 _LockSafeConnection 同款 —— 只退**本函数自己开的**
    # 事务, 调用方先 BEGIN 的待写内容不夺。
    was_in_tx = conn.in_transaction

    # B1: 推荐池条件单到期未触发 → 降级回跟踪池 (附降级原因)
    rows = conn.execute(
        "SELECT code, valid_until, entry_high FROM stock_pool "
        "WHERE status='pending_entry' AND valid_until IS NOT NULL").fetchall()
    try:
        for r in rows:
            vu = str(r[1])[:10]
            if t and vu and t > vu:
                # AUD-029: **留痕先于状态迁移**。旧序先 UPDATE 后 INSERT ——
                # UPDATE 先开了事务, INSERT 失败时 was_in_tx 已为真, 底层不回滚,
                # 那条 UPDATE 就悬在未提交事务里等别人 commit = 改了没留痕。
                conn.execute(
                    "INSERT INTO action_records (code, action_type, reason, source) "
                    "VALUES (?, 'pool_downgrade', ?, 'execution')",
                    (r[0], f"推荐池→跟踪池: 条件单到期未触发 (valid_until={vu})"))
                conn.execute(
                    f"UPDATE stock_pool SET status='watch', {null_sql}, "
                    "watch_reason=? WHERE code=?",
                    (f"条件单到期未触发降级回跟踪池 (valid_until={vu})", r[0]))
                n_moved += 1
    except Exception:
        if not was_in_tx and conn.in_transaction:
            conn.rollback()
        raise
    if n_moved:
        conn.commit()

    # B2/B3: 跟踪池退出三规则
    exited = 0
    rows = conn.execute(
        "SELECT code, entry_low, last_price, latest_signal_date, valid_until "
        "FROM stock_pool WHERE status='watch'").fetchall()
    was_in_tx = conn.in_transaction
    try:
        for r in rows:
            code = r[0]
            el = float(r[1] or 0)
            # 列序: code / entry_low / last_price / latest_signal_date / valid_until
            lp = float(r[2] or 0)
            lsd = str(r[3] or "")[:10]
            reason = None
            # 规则1: 跌破观察期低点 X% (观察期低点≈入场区间下沿; last_price 为现值)
            if el > 0 and lp > 0 and lp <= el * (1 - cfg["obs_low_drop_pct"] / 100.0):
                reason = (f"跌破观察期低点 {cfg['obs_low_drop_pct']}%: "
                          f"现价 {lp} << 低点 {el}")
            # 规则2: 成交额萎缩 (数据面 09-11 起有数 — 首版以信号面替代:
            # latest_signal_date 超 N 日无信号即萎缩同判)
            elif lsd and t and lsd < t:
                from datetime import datetime
                try:
                    d_days = (datetime.strptime(t, "%Y-%m-%d")
                              - datetime.strptime(lsd, "%Y-%m-%d")).days
                except ValueError:
                    d_days = 0
                if d_days > cfg["no_signal_days"]:
                    reason = f"连续 {d_days} 日无信号 (> N={cfg['no_signal_days']})"
            if reason:
                # AUD-029: 同 B1 —— 留痕先写, 状态后改。
                conn.execute(
                    "INSERT INTO action_records (code, action_type, reason, source) "
                    "VALUES (?, 'pool_exit', ?, 'execution')", (code, reason))
                conn.execute(
                    "UPDATE stock_pool SET status='exited', exit_reason=?, "
                    "exit_reason_detail=?, exit_layer=0 WHERE code=?",
                    (reason, "跟踪池退出 (advisor pool_maintenance)", code))
                exited += 1
    except Exception:
        if not was_in_tx and conn.in_transaction:
            conn.rollback()
        raise
    if exited:
        conn.commit()
    return n_moved + exited


# ---------------------------------------------------------------------------
# C. 推荐位收口
# ---------------------------------------------------------------------------

def promote_recommendations(db, watch_candidates: list[dict],
                            n_target: int = 5, today: str = "",
                            config: dict | None = None) -> dict:
    """推荐位收口: pool-first + composite 补位 + risk 否决 + 断层浮动。

    watch_candidates: 策略面新候选 [{code, composite_score, entry_low, entry_high}]
    返回 {promoted, backfilled, vetoed, n_final}。
    """
    conn = db.get_connection()
    t = (today or "")[:10]
    n_cap = clamp_n(n_target)
    promoted, backfilled, vetoed = [], [], []

    # 候选池 = 跟踪池内现有 (pool-first) + 新 watch_candidates
    watch_rows = conn.execute(
        "SELECT code, current_score, entry_low, entry_high FROM stock_pool "
        "WHERE status='watch'").fetchall()
    pool_cands = [
        {"code": r[0], "composite_score": float(r[1] or 0),
         "entry_low": r[2], "entry_high": r[3], "_from_pool": True}
        for r in watch_rows]
    new_cands = [dict(c, _from_pool=False) for c in (watch_candidates or [])]
    all_cands = pool_cands + new_cands

    # risk 前置否决 + 排序 (pool-first: 池内优先, 同优先按分数)
    survived = []
    for c in all_cands:
        risk = _latest_risk(conn, c["code"], t)
        if risk and risk[0] in RISK_VETO_VERDICTS and risk[1] > RISK_VETO_CONF:
            vetoed.append({"code": c["code"], "verdict": risk[0], "conf": risk[1]})
            continue
        survived.append(c)
    survived.sort(key=lambda c: (not c["_from_pool"], -float(c["composite_score"] or 0)))

    # 断层浮动: 相邻候选分差 > GAP_RATIO → 截断
    picked = []
    for i, c in enumerate(survived):
        if len(picked) >= n_cap:
            break
        if picked:
            prev_score = float(picked[-1]["composite_score"] or 0)
            cur_score = float(c["composite_score"] or 0)
            if prev_score > 0 and (prev_score - cur_score) / prev_score > GAP_RATIO:
                break  # 断层: 不带低分尾巴
        picked.append(c)

    # 落池: 升推荐 (池内 → pending_entry; 新候选 → 插入 pending_entry)
    for c in picked:
        if c["_from_pool"]:
            # AUD-027: 绕行路径② —— 同上, 池内升推荐也是一次「复活」。
            from execution_layer.core.stock_pool import exit_fields_null_sql
            conn.execute(
                f"UPDATE stock_pool SET status='pending_entry', {exit_fields_null_sql()}, "
                "entry_low=COALESCE(entry_low, ?), entry_high=COALESCE(entry_high, ?), "
                "valid_until=? WHERE code=?",
                (c.get("entry_low") or 0, c.get("entry_high") or 0,
                 f"{t}+10d" if t else None, c["code"]))
            promoted.append(c["code"])
        else:
            conn.execute(
                "INSERT OR REPLACE INTO stock_pool "
                "(code, name, status, source, added_date, initial_score, "
                "entry_low, entry_high, valid_until) "
                "VALUES (?, ?, 'pending_entry', 'strategy', ?, ?, ?, ?, ?)",
                (c["code"], c.get("name") or c["code"], t,
                 float(c.get("composite_score") or 0),
                 c.get("entry_low") or 0, c.get("entry_high") or 0, None))
            backfilled.append(c["code"])
    conn.commit()
    return {"promoted": promoted, "backfilled": backfilled,
            "vetoed": vetoed, "n_final": len(picked)}


# ---------------------------------------------------------------------------
# D. 盘中域收窄 (性能顺手件 — monitor 消费)
# ---------------------------------------------------------------------------

def scan_codes(db) -> list[str]:
    """intraday 优先域: 推荐+跟踪+持仓 (active 仍须盯 — 持仓面不可缩)。"""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT code FROM stock_pool WHERE status IN "
        "('pending_entry', 'watch', 'active')").fetchall()
    return [r[0] for r in rows]
