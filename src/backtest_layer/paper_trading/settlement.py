"""EX-1/EX-3: benchmark + 持仓快照日终结算 — 18:05 fx_settlement 窗。

病根 (2026-09-10 取证): D8 (324c2df) 的 16:35 reconcile 路径在生产 13 天
零落地 (PTS 十行 benchmark_return 全 NULL), 同代码同库手动复跑非 None —
病在 16:35 cron 时序环境 (锁竞争/双 bootstrap 竞态), 不在计算逻辑。
工程裁定: 不复现竞态, 改在 18:05 fx_settlement 日终窗补写 — 彼时
index_daily 当日行 100% 已在 (ddp 15:47~15:59 子管道 + 17:40 kline 兜底
双保险), 且 16:35 锁堵 skipped 的日子也能补 (UPDATE 行不存在则 INSERT)。

EX-3 同窗加腿: paper_positions_snapshot 日终持仓快照 (期4 半通电面
根治 — stock_pool 只有现值无历史)。

口径 (与 D8 完全一致): 当日收益 = close(T)/close(T-1) - 1, 四位小数舍入。
诚实降级 (铁律 #12): 指数缺数/无 data_layer/读失败 → 不写假值, reason
留痕, 不抛错 (不拖垮 fx_settlement 主路径)。
幂等: 已有真值不覆盖; 历史欠账 backfill_days 参数逐日补齐 (仅 NULL 行)。
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("backtest.paper_settlement")

DEFAULT_BENCHMARK_CODE = "000300.SH"


def snapshot_positions_for_date(db, date: str) -> dict:
    """EX-3: 日终持仓快照 — stock_pool active 行 → paper_positions_snapshot。

    幂等: PK (date, code) + INSERT OR REPLACE (晚到的日终价最后一跑为准)。
    weight = 该仓 mv / Σ(active mv) 仓内占比 (现金不进表)。
    降级: 无活仓 → 诚实空 (零行); last_price 缺 (从未盯市) → 该票跳过
    留痕 skipped, 不造 mv=0 假行。
    """
    result: dict = {"rows": 0, "skipped": 0}
    if db is None:
        result["reason"] = "no_db"
        return result
    conn = db.get_connection()
    try:
        rows = conn.execute(
            # 卡E (2026-10-07, 派单 sm-695aabac1b9640bd) 镜像包A: status 筛选
            # 覆 exit_pending+pending_entry — stock_pool.status 真实值域无 'active'
            # 稳态 (T+1 结算当日转 exit_pending)，不覆则快照漏全部持仓
            # (生产实证 max(date)=09-25 后 12 天零新增)。
            "SELECT code, position_qty, entry_price, last_price, unrealized_pnl "
            "FROM stock_pool "
            "WHERE status IN ('active', 'exit_pending', 'pending_entry')").fetchall()
    except Exception as e:
        result["reason"] = "read_failed"
        logger.warning(f"持仓快照读取失败: {e}")
        return result
    if not rows:
        return result
    # 先算 mv, 缺价的票跳过 (不进分母 — 它没有市值事实)
    valued = []
    for r in rows:
        qty = r["position_qty"] or 0
        price = r["last_price"]
        if not price or not qty:
            result["skipped"] += 1
            continue
        valued.append((r, qty, price))
    if not valued:
        return result
    total_mv = sum(qty * price for _, qty, price in valued)
    try:
        for r, qty, price in valued:
            mv = round(qty * price, 2)
            weight = round(qty * price / total_mv, 4) if total_mv else 0.0
            entry = r["entry_price"]
            pnl = r["unrealized_pnl"]
            if pnl is None and entry:
                pnl = round((price - entry) * qty, 2)
            conn.execute(
                "INSERT OR REPLACE INTO paper_positions_snapshot "
                "(date, code, shares, mv, weight, entry_price, unrealized_pnl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (date, r["code"], qty, mv, weight, entry, pnl))
        conn.commit()
        result["rows"] = len(valued)
        logger.info(f"持仓快照 {date}: {len(valued)} 仓写入"
                    + (f", 跳过 {result['skipped']} (无价)" if result["skipped"] else ""))
    except Exception as e:
        result["reason"] = "write_failed"
        logger.warning(f"持仓快照写入失败: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    return result


def _benchmark_daily_return(data_layer, code: str, date: str) -> tuple[float | None, str | None]:
    """D8 同款口径: 当日收益 = close(T)/close(T-1) - 1。缺数 → (None, reason) 不造假。"""
    if data_layer is None:
        return None, "no_data_layer"
    try:
        rows = data_layer.get_index_daily(code, end_date=date)
    except Exception:
        return None, "index_read_failed"
    if not rows or len(rows) < 2:
        return None, "index_missing_history"
    last, prev = rows[-1], rows[-2]
    if last.get("date") != date or not last.get("close") or not prev.get("close"):
        return None, "index_missing_current"
    return round(last["close"] / prev["close"] - 1, 6), None


def settle_benchmark_for_date(db, data_layer, date: str,
                               config=None, backfill_days: int = 0) -> dict:
    """结算单日 (可选回补历史): UPDATE 当日 PTS 行 benchmark_return。

    返回 {written, reason?}: written=True 表示有写入 (UPDATE 或 INSERT);
    降级时 written=False + reason 留痕。绝不抛错 — 调用方 (fx_settlement
    尾腿) 的主路径 (因子补算) 不因基准缺数而失败。
    """
    result: dict = {"written": False}
    code = DEFAULT_BENCHMARK_CODE
    if config is not None and hasattr(config, "get"):
        code = config.get("backtest.paper_trading.benchmark_code", code) or code

    dates = [date]
    if backfill_days > 0:
        # 回补窗口: 当日往前 backfill_days 个自然日, 仅补 NULL 行
        from datetime import datetime, timedelta
        try:
            d0 = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            d0 = None
        if d0 is not None:
            for i in range(1, backfill_days + 1):
                dates.append((d0 - timedelta(days=i)).strftime("%Y-%m-%d"))

    conn = db.get_connection() if db is not None else None
    if conn is None:
        result["reason"] = "no_db"
        return result

    wrote_any = False
    for d in dates:
        ret, why = _benchmark_daily_return(data_layer, code, d)
        if ret is None:
            if d == date:
                result["reason"] = why
            continue
        try:
            row = conn.execute(
                "SELECT id, benchmark_return FROM paper_trading_stats WHERE date = ?",
                (d,)).fetchone()
            if row is None:
                # 16:35 锁堵 skipped 日兜底: 信号面未知诚实 NULL, 只填基准面
                conn.execute(
                    "INSERT INTO paper_trading_stats "
                    "(date, total_signals, filled_signals, paper_return, benchmark_return) "
                    "VALUES (?, NULL, NULL, NULL, ?)", (d, ret))
                wrote_any = True
                logger.info(f"benchmark 结算: {d} 补行 INSERT benchmark_return={ret}")
            elif row["benchmark_return"] is None:
                conn.execute(
                    "UPDATE paper_trading_stats SET benchmark_return = ? WHERE id = ?",
                    (ret, row["id"]))
                wrote_any = True
                logger.info(f"benchmark 结算: {d} UPDATE benchmark_return={ret}")
            # 已有真值 → 幂等跳过 (不覆盖)
        except Exception as e:
            if d == date:
                result["reason"] = "write_failed"
            logger.warning(f"benchmark 结算 {d} 写入失败: {e}")
            continue
    if wrote_any:
        try:
            conn.commit()
        except Exception as e:
            logger.warning(f"benchmark 结算 commit 失败: {e}")
            return {"written": False, "reason": "commit_failed"}
        return {"written": True}
    if "reason" not in result and not wrote_any:
        result["reason"] = result.get("reason") or "already_settled"
    return result


def settle_daily(db, data_layer, date: str, config=None, backfill_days: int = 0) -> dict:
    """EX-1+EX-3 日终结算总入口 (fx_settlement 18:05 窗单点调用)。

    两腿: ①benchmark_return 补写 ②持仓快照落表。
    各腿独立降级 (互不拖累): 一腿失败另一腿照写, 汇总 metadata 留痕。
    """
    bench = settle_benchmark_for_date(db, data_layer, date,
                                       config=config, backfill_days=backfill_days)
    snap = snapshot_positions_for_date(db, date)
    return {"benchmark": bench, "snapshot": snap}
