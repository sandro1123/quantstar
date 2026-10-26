"""P0-3: factor_daily 缺口回填脚本 (版本受控, 取代一次性 ad-hoc 脚本)。

背景: 2026-08-19/20/21 三天 factor_daily 为空 (08-18 有 5540 行)。根因是
daily_data_pipeline ghost 进程持有 qs.db 写锁, factor_update 撞锁失败且
OPS-04 静默丢记录 (P0-5 已修)。本脚本按日重算并落库因子, 补齐缺口。

设计要点 (对齐任务卡):
- `if __name__ == '__main__':` 保护, 可被安全 import。
- 自带 load_dotenv (cron 直驱不经 src/__main__.py, 与 P0-1 同款)。
- flock 文件锁: 单实例, 撞锁即退出不排队 (铁律 #4 串行写库)。
- --dry-run 默认 / --apply 显式: 未加 --apply 只报告缺口与工作量, 零写入。
- 按日可恢复: 已有 factor_daily 行的日期自动跳过。
- 不设 OMP_NUM_THREADS (铁律 #6)。
- 只复现 run_daily 的 stage 1-2 (factor_compute + aggregate + 持久化),
  不跑 ml_score/select/agent_gate/signal_gen — 避免对历史日期污染
  model_predictions / stock_pool / agent_predictions。
- 可观测 (铁律 #12): 每日事件写 logs/backfill_factors.jsonl + stdout。

用法:
    PYTHONPATH=src .venv/bin/python scripts/backfill_factors.py --start 2026-08-19 --end 2026-08-21
    # 确认 dry-run 输出无误后, 报统筹拿 GO, 再加 --apply 实际写入
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env() -> None:
    """cron 直驱入口自带 load_dotenv (与 src/__main__.py:12 同款, P0-1/P0-3)。"""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def _emit(log_path: str, event: str, **payload) -> None:
    record = {"event": event, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **payload}
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # 观测失败不阻断回填, 但必须出声 (铁律 #12)
        print(f"[backfill_factors][LOG-FAILED] {e}", file=sys.stderr, flush=True)


def _acquire_lock() -> "file":
    """flock 单实例锁; 已被占用立即退出 (铁律 #4 串行写库, 不排队)。"""
    lock_path = os.path.join(PROJECT_ROOT, "logs", "backfill_factors.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[backfill_factors] 已有实例在运行 (flock 占用), 退出以避免并发写库。",
              file=sys.stderr, flush=True)
        sys.exit(3)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _trading_dates(conn, start: str, end: str) -> list[str]:
    """以 daily_klines 为准枚举 [start, end] 内的交易日。"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_klines WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true",
                        help="实际写入 (默认 dry-run 只报告, 零写入)")
    parser.add_argument("--db", default=None,
                        help="目标库路径 (默认 Database() 解析; 从 worktree 运行时须显式指向生产库)")
    parser.add_argument("--workers", type=int, default=0,
                        help="因子并行 worker 数 (0=自动 min(cpu,4))")
    args = parser.parse_args()

    _load_env()  # 必须先于各层导入 (CF-01 启动校验依赖 .env)

    from common.database import Database
    from data_layer import DataLayer
    from strategy_layer import StrategyLayer
    from strategy_layer.feedback.persistence import WeightPersistence

    log_path = os.path.join(PROJECT_ROOT, "logs", "backfill_factors.jsonl")
    mode = "apply" if args.apply else "dry-run"
    _emit(log_path, "run_started", mode=mode, start=args.start, end=args.end,
          db=args.db or "(default)", pid=os.getpid())

    lock_file = _acquire_lock()  # noqa: F841  持有至进程退出
    db = Database(args.db) if args.db else Database()
    conn = db.get_connection()

    dates = _trading_dates(conn, args.start, args.end)
    if not dates:
        _emit(log_path, "no_trading_dates", start=args.start, end=args.end)
        return 1

    # 已有 factor_daily 行的日期跳过 (按日可恢复)
    pending = []
    for d in dates:
        n = conn.execute("SELECT COUNT(*) FROM factor_daily WHERE date = ?", (d,)).fetchone()[0]
        if n > 0:
            _emit(log_path, "date_skipped", date=d, existing_rows=n)
        else:
            pending.append(d)

    if not pending:
        _emit(log_path, "all_dates_present", dates=dates)
        return 0

    if not args.apply:
        # dry-run: 只报告缺口与预估工作量, 不构建重对象, 零写入
        for d in pending:
            kline_n = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM daily_klines WHERE date = ?", (d,)).fetchone()[0]
            _emit(log_path, "dry_run_gap", date=d, kline_codes=kline_n)
        _emit(log_path, "dry_run_summary", pending_dates=pending,
              hint="确认无误后报统筹拿 GO, 再加 --apply 实际写入")
        return 0

    # ---- apply: 构建策略层, 按日重算 stage 1-2 并落库 ----
    data_layer = DataLayer(db)
    strategy = StrategyLayer(data_layer=data_layer, db=db, mode="backtest")
    n_workers = args.workers or max(1, min(os.cpu_count() or 1, 4))
    wp = WeightPersistence(config=None, db=db)
    persisted = wp.load_weights(default_weights=None)
    weights = persisted if persisted else None

    failures = 0
    for d in pending:
        t0 = time.monotonic()
        try:
            codes = strategy._get_stock_codes(d)
            if not codes:
                _emit(log_path, "date_no_codes", date=d)
                failures += 1
                continue
            matrix = strategy.factor_engine.compute_all(codes, d, workers=n_workers)
            factor_daily = strategy.aggregator.aggregate(
                matrix, weights=weights, use_correlation_filter=True)
            if len(factor_daily) != len(codes):
                raise RuntimeError(
                    f"coverage mismatch {d}: codes={len(codes)} aggregated={len(factor_daily)}")
            for fd in factor_daily:
                data_layer.update_factor_daily(
                    fd.code, fd.date, fd.factor_scores,
                    rank=getattr(fd, "rank", 0), quantile=getattr(fd, "quantile", 0))
            written = conn.execute(
                "SELECT COUNT(*) FROM factor_daily WHERE date = ?", (d,)).fetchone()[0]
            if written == 0:
                raise RuntimeError(f"factor_daily still empty after write for {d}")
            _emit(log_path, "date_completed", date=d, rows=written,
                  elapsed_s=round(time.monotonic() - t0, 1))
        except Exception as e:
            failures += 1
            _emit(log_path, "date_failed", date=d, error_type=type(e).__name__, message=str(e))

    _emit(log_path, "run_finished", mode=mode, pending=len(pending),
          completed=len(pending) - failures, failures=failures)
    db.close()
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
