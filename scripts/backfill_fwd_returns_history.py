"""DL-34: fwd_return 历史全量回补脚本 (版本受控, dry-run 默认, 写库需 GO-9)。

背景: DL-24 建了 backfill_fwd_returns 按日回填入口, 但从未排进每日调度, 导致
factor_daily.fwd_return_5d 停在 2026-08-13、fwd_return_20d 近段断档。每日增量由
ops 管道 fwd_return_backfill (本批新增) 负责; 本脚本负责一次性补齐历史缺口。

实测 (2026-08-22, 生产库): fwd_return_20d 并非"全表 0 覆盖"——已有 381 万行非空
(2010-01-04 ~ 2026-07-23)。真实缺口是"已兑现但仍为 NULL"的行: 5d 约 311 万行
(1178 个日期)、20d 约 312 万行 (1173 个日期)。本脚本只补这部分, 幂等。

设计要点 (对齐 P0-3 backfill_factors.py):
- `if __name__ == '__main__':` 保护, 可被安全 import。
- 自带 load_dotenv (cron 直驱不经 src/__main__.py)。
- flock 文件锁: 单实例, 撞锁即退出不排队 (铁律 #4 串行写库)。
- --dry-run 默认 / --apply 显式: 未加 --apply 只报告缺口, 零写入。
- 按日可恢复: 复用 backfill_fwd_returns 的"仅写 NULL 行"幂等性, 中断重跑安全。
- 不设 OMP_NUM_THREADS。
- 可观测 (铁律 #12): 事件写 logs/backfill_fwd_returns.jsonl + stdout。
- 复用 data_layer.backfill_fwd_returns (DL-24 语义: 未兑现保持 NULL, 百分点单位),
  不重写回填逻辑。

用法:
    PYTHONPATH=src .venv/bin/python scripts/backfill_fwd_returns_history.py
    # dry-run 报告缺口; 确认后报统筹拿 GO-9, 再加 --apply 实际写入
    PYTHONPATH=src .venv/bin/python scripts/backfill_fwd_returns_history.py --apply --db ~/qs/data/qs.db
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# fwd_10d: 引 data_layer 单一事实源, 不再复制字面量 (断裂五形第五形收编)。
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
from data_layer._loader import FWD_HORIZONS as _SRC_HORIZONS
from data_layer._loader import FWD_HORIZON_COLS as _SRC_HORIZON_COLS

HORIZONS = tuple(_SRC_HORIZONS)
HORIZON_COLS = dict(_SRC_HORIZON_COLS)


def _load_env() -> None:
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
    except Exception as e:  # 观测失败不阻断, 但必须出声 (铁律 #12)
        print(f"[backfill_fwd][LOG-FAILED] {e}", file=sys.stderr, flush=True)


def _acquire_lock():
    lock_path = os.path.join(PROJECT_ROOT, "logs", "backfill_fwd_returns.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[backfill_fwd] 已有实例在运行 (flock 占用), 退出以避免并发写库。",
              file=sys.stderr, flush=True)
        sys.exit(3)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _gap_dates(conn) -> list[str]:
    """factor_daily 中任一 horizon 为 NULL 的日期 (升序)。

    backfill_fwd_returns 自带"未兑现不写"的 EXISTS 守卫, 故无需在此预判
    每个日期每个 horizon 是否已兑现 — 直接对缺口日期调回填, 幂等安全。
    """
    conds = " OR ".join(f"{c} IS NULL" for c in HORIZON_COLS.values())
    rows = conn.execute(
        f"SELECT DISTINCT date FROM factor_daily WHERE {conds} ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def build_arg_parser() -> argparse.ArgumentParser:
    """argparse 拆出 (可测性; 原为 main 内联, 零默认行为变更)。

    --skip-kline-backed (task#34): gap_summary 增算 kline-backed 可填上界
    默认算; 显式跳过给未来 apply 场景留快路 (五列无索引 EXISTS 面分钟级,
    重跑 apply 若只关心 NULL 总量可跳过)。跳过路径 kline_backed/structural
    键在但值为 None — 消费方能区分「没算」vs「没这键」(只增不减)。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="实际写入 (默认 dry-run 只报告缺口, 零写入)")
    parser.add_argument("--db", default=None,
                        help="目标库路径 (从 worktree 运行时须显式指向生产库)")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 个缺口日期 (0=全部, 用于分批/试跑)")
    parser.add_argument("--horizons", type=str, default=None,
                        help="只回填指定周期, 逗号分隔 (如 '5'); 默认全部。"
                             "用于优先级分批: 5d 是 ic_decay/agent_evaluate 直接输入,"
                             "一夜跑不完时先 5 后其余。")
    parser.add_argument("--skip-kline-backed", action="store_true",
                        help="gap_summary 跳过 kline-backed 可填上界计算 "
                             "(默认算; 跳过时事件键在值为 None)")
    return parser


def _gap_split_by_horizon(conn, horizon_cols, skip_kline_backed=False):
    """缺口三口径拆分 (task#34, 原 gap_rows 循环提取, 纯提取零逻辑变)。

    每 horizon 报三口径:
    - null          = 该列 IS NULL 行数 (原 null_rows_by_horizon 同口径)
    - kline_backed  = NULL 且 EXISTS daily_klines(code,date) 匹配 — 可填上界
                      (判据与 GO-9 销案对拍 bit-identical)
    - structural    = null - kline_backed — 结构性不可填 (无 kline 行, 回填器
                      EXISTS 守卫正确拒写; GO-9 根因「NULL≈可填」前提不成立
                      的量化面)
    skip_kline_backed=True 时 kline_backed/structural 置 None (不算而非没键,
    null 恒算)。
    """
    out = {}
    for h, col in horizon_cols.items():
        null_n = conn.execute(
            f"SELECT COUNT(*) FROM factor_daily WHERE {col} IS NULL"
        ).fetchone()[0]
        if skip_kline_backed:
            out[h] = {"null": null_n, "kline_backed": None, "structural": None}
            continue
        k_n = conn.execute(
            f"SELECT COUNT(*) FROM factor_daily fd WHERE fd.{col} IS NULL "
            f"AND EXISTS (SELECT 1 FROM daily_klines k "
            f"WHERE k.code=fd.code AND k.date=fd.date)"
        ).fetchone()[0]
        out[h] = {"null": null_n, "kline_backed": k_n,
                  "structural": null_n - k_n}
    return out


def main() -> int:
    args = build_arg_parser().parse_args()

    _load_env()

    from common.database import Database
    from data_layer._loader import DataLoader

    global HORIZONS, HORIZON_COLS
    if args.horizons:
        try:
            wanted = tuple(int(x) for x in args.horizons.split(",") if x.strip())
        except ValueError:
            print(f"[backfill_fwd] --horizons 解析失败: {args.horizons!r}",
                  file=sys.stderr)
            return 2
        bad = [h for h in wanted if h not in _SRC_HORIZON_COLS]
        if not wanted or bad:
            print(f"[backfill_fwd] 非法周期 {bad}; 可用: {sorted(FWD_HORIZON_COLS)}",
                  file=sys.stderr)
            return 2
        HORIZONS = wanted
        HORIZON_COLS = {h: _SRC_HORIZON_COLS[h] for h in wanted}

    log_path = os.path.join(PROJECT_ROOT, "logs", "backfill_fwd_returns.jsonl")
    mode = "apply" if args.apply else "dry-run"
    _emit(log_path, "run_started", mode=mode, db=args.db or "(default)", pid=os.getpid())

    lock_file = _acquire_lock()  # noqa: F841  持有至进程退出
    db = Database(args.db) if args.db else Database()
    conn = db.get_connection()

    dates = _gap_dates(conn)
    if args.limit > 0:
        dates = dates[: args.limit]

    if not dates:
        _emit(log_path, "no_gap", hint="factor_daily 无 NULL fwd_return, 无需回补")
        return 0

    # 缺口规模预估 (dry-run 与 apply 都报告)
    # task#34: 三口径拆分 (null / kline_backed 可填上界 / structural 结构性不可填)
    split = _gap_split_by_horizon(conn, HORIZON_COLS,
                                  skip_kline_backed=args.skip_kline_backed)
    gap_rows = {h: v["null"] for h, v in split.items()}
    kline_rows = {h: v["kline_backed"] for h, v in split.items()}
    structural_rows = {h: v["structural"] for h, v in split.items()}
    _emit(log_path, "gap_summary", gap_dates=len(dates),
          first=dates[0], last=dates[-1], null_rows_by_horizon=gap_rows,
          kline_backed_by_horizon=kline_rows,
          structural_by_horizon=structural_rows)

    if not args.apply:
        _emit(log_path, "dry_run_done",
              hint="确认缺口后报统筹拿 GO-9, 再加 --apply --db <生产库> 实际写入")
        db.close()
        return 0

    # ---- apply: 逐日回填 (幂等, 仅写 NULL 且已兑现的行) ----
    loader = DataLoader(db)
    total_filled = {h: 0 for h in HORIZONS}
    failures = 0
    for i, d in enumerate(dates):
        t0 = time.monotonic()
        try:
            filled = loader.backfill_fwd_returns(d, horizons=HORIZONS)
            for h in HORIZONS:
                total_filled[h] += int(filled.get(h, 0) or 0)
            if (i + 1) % 50 == 0 or i == len(dates) - 1:
                _emit(log_path, "progress", done=i + 1, total=len(dates),
                      filled_so_far=dict(total_filled),
                      elapsed_s=round(time.monotonic() - t0, 1))
        except Exception as e:
            failures += 1
            _emit(log_path, "date_failed", date=d,
                  error_type=type(e).__name__, message=str(e))

    _emit(log_path, "run_finished", mode=mode, dates=len(dates),
          filled_by_horizon=dict(total_filled), failures=failures)
    db.close()
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
