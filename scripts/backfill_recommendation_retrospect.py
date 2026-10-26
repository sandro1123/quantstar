"""P0-1 推荐回溯表回填脚本：把 agent_predictions 每条推荐建成可回溯档案.

背景（统筹派单 v2 对标 P0-1）：
  625+ 条存量推荐零评估——agent_evaluate 作业虽在 ops.yaml 排程但从未执行，
  actual_return/hit 全 NULL。本脚本为每条推荐建立 recommendation_retrospect 档案：
    * 推荐当时的 trace：model_predictions 三分 + agent_memory reasoning +
      factor_daily 当日因子快照（排除 fwd_return_* 未来列，防泄漏）
    * 事后 score：factor_daily.fwd_return_5d/10d（DL-24 正源），百分数÷100 折小数
    * hit 判定：AG-01 统一口径（evaluator.py），非 agent_eval.is_hit 旧口径

设计要点：
  * 锚点 = agent_predictions.id（prediction_id 1:1），INSERT 幂等
  * trace 一次性建档不可变（推荐当时的快照）；score/hit 增量结算（未结算行补回填）
  * 单位统一小数：agent_predictions.confidence 本是小数；fwd_return_* 百分数÷100
  * hit 口径：推荐+actual>0 / 不推荐+actual<0 → 命中；观望·警惕 + |actual|<2% → 命中
    注：agent_eval.is_hit(BT-09) 把警惕一律记 miss，与本口径不一致——已报统筹裁决，
    本脚本以 AG-01 统一口径（evaluator.py）为准
  * verdict 经 normalize_verdict 归一后再判 hit（legacy 兼容，同 evaluator 用法）
  * 空 date 脏行跳过留痕（1 条，id 已知）
  * 铁律#4：flock 单实例锁，拿不到立即退出，绝不排队（仅 --apply 持锁；dry-run 只读）
  * 铁律#11：自载 .env
  * 铁律#12：jsonl 事件留痕 logs/backfill_recommendation_retrospect.jsonl

用法：
  # dry-run（默认，零写库）：出覆盖/到期统计 + 样例档案
  PYTHONPATH=src .venv/bin/python scripts/backfill_recommendation_retrospect.py

  # 真跑（统筹 GO 后）
  PYTHONPATH=src .venv/bin/python scripts/backfill_recommendation_retrospect.py --apply
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "backfill_recommendation_retrospect.jsonl")
LOCK_PATH = os.path.join(PROJECT_ROOT, "logs", "backfill_recommendation_retrospect.lock")

_TZ_CN = timezone(timedelta(hours=8))

# #112 canonical 收敛: is_hit_ag01 本地第二副本已删 — 唯一实现点为
# agent_layer.quality.evaluator.is_hit_ag01 (AG-01, 与 Evaluator.evaluate 同源).
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
from agent_layer.quality.evaluator import is_hit_ag01  # noqa: E402,F401

DDL = """\
CREATE TABLE IF NOT EXISTS recommendation_retrospect (
    prediction_id   INTEGER PRIMARY KEY REFERENCES agent_predictions(id),
    request_id      TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    code            TEXT NOT NULL,
    rec_date        TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    confidence      REAL,
    ml_score        REAL,
    factor_score    REAL,
    blended_score   REAL,
    model_version   TEXT,
    reasoning       TEXT,
    trace_json      TEXT,
    fwd_return_5d   REAL,
    fwd_return_10d  REAL,
    hit_5d          INTEGER,
    hit_10d         INTEGER,
    settled_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
)"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_reco_retro_date ON recommendation_retrospect(rec_date)",
    "CREATE INDEX IF NOT EXISTS idx_reco_retro_code ON recommendation_retrospect(code, rec_date)",
    "CREATE INDEX IF NOT EXISTS idx_reco_retro_unsettled ON recommendation_retrospect(settled_at) WHERE settled_at IS NULL",
]

# factor_daily 中的未来数据列前缀——trace 快照必须排除（防泄漏）
FWD_PREFIX = "fwd_return_"
# factor_daily 中不属于"当日因子快照"的主键列
FACTOR_KEY_COLS = {"date", "code"}


def _now_cn() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _load_env() -> None:
    """铁律#11：自载 .env（生产=dotenv 直驱，不依赖 shell export）"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)


def _emit(event: str, **payload) -> None:
    """铁律#12：jsonl 事件留痕（stdout + logs/*.jsonl）"""
    rec = {"ts": _now_cn(), "event": event, **payload}
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _acquire_lock():
    """铁律#4：flock 单实例，拿不到立即退出（仅 --apply 持锁）"""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _emit("lock_held_abort", lock=LOCK_PATH)
        sys.exit("❌ 另一实例持锁中，本次退出（铁律#4 不排队）")
    fh.write(f"pid={os.getpid()} started={_now_cn()}\n")
    fh.flush()
    return fh


def _db_path() -> str:
    return os.environ.get("DB_PATH") or os.path.join(PROJECT_ROOT, "data", "qs.db")


def _open_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn



def _factor_snapshot_columns(conn: sqlite3.Connection) -> list[str]:
    """PRAGMA 驱动取 factor_daily 因子列，排除主键列与 fwd_return_* 未来列"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(factor_daily)")]
    return [c for c in cols if c not in FACTOR_KEY_COLS and not c.startswith(FWD_PREFIX)]


def _load_sources(ro: sqlite3.Connection, factor_cols: list[str]):
    """批量取三路 trace 源 + 事后收益，全部 1:1 键（已实测验证）"""
    preds = [
        dict(r)
        for r in ro.execute(
            "SELECT id, request_id, agent_name, code, date, verdict, confidence "
            "FROM agent_predictions ORDER BY id"
        )
    ]
    # model_predictions：PK (date, code) 唯一，LEFT JOIN 无重（197/197 实测 count=1）
    model = {
        (r["date"], r["code"]): dict(r)
        for r in ro.execute(
            "SELECT p.date, p.code, m.ml_score, m.factor_score, m.blended_score, m.model_version "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN model_predictions m ON m.date = p.date AND m.code = p.code"
        )
    }
    # agent_memory：PK (code, date) 唯一，覆盖 685/685
    memory = {
        (r["date"], r["code"]): r["reasoning"]
        for r in ro.execute(
            "SELECT am.code, am.date, am.reasoning "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN agent_memory am ON am.date = p.date AND am.code = p.code"
        )
    }
    # factor_daily 当日因子快照 + 事后 fwd（同一行取两类列，查询分两份语义清晰）
    fcols_sql = ", ".join(f"f.{c}" for c in factor_cols)
    factors: dict[tuple[str, str], dict] = {}
    fwd: dict[tuple[str, str], dict] = {}
    for r in ro.execute(
        f"SELECT f.date, f.code, {fcols_sql} "
        "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
        "JOIN factor_daily f ON f.date = p.date AND f.code = p.code"
    ):
        d = dict(r)
        key = (d.pop("date"), d.pop("code"))
        factors[key] = d
    for r in ro.execute(
        "SELECT f.date, f.code, f.fwd_return_5d, f.fwd_return_10d "
        "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
        "JOIN factor_daily f ON f.date = p.date AND f.code = p.code"
    ):
        fwd[(r["date"], r["code"])] = {
            "fwd_return_5d": r["fwd_return_5d"],
            "fwd_return_10d": r["fwd_return_10d"],
        }
    return preds, model, memory, factors, fwd


def _build_row(
    pred: dict,
    model_row: dict | None,
    reasoning: str | None,
    factor_row: dict | None,
    fwd_row: dict | None,
    norm_verdict_fn,
    trace_built_at: str,
) -> dict:
    """组一条档案：trace（一次性）+ score/hit（增量）"""
    sources = ["agent_predictions"]
    model_part = None
    if model_row is not None:
        sources.append("model_predictions")
        model_part = {
            "ml_score": model_row.get("ml_score"),
            "factor_score": model_row.get("factor_score"),
            "blended_score": model_row.get("blended_score"),
            "model_version": model_row.get("model_version"),
        }
    if reasoning is not None:
        sources.append("agent_memory")
    factor_part = None
    if factor_row is not None:
        sources.append("factor_daily")
        factor_part = dict(factor_row)
        # factor_scores 列是 JSON 文本，解包成对象（失败留原串）
        fs_raw = factor_part.get("factor_scores")
        if isinstance(fs_raw, str) and fs_raw:
            try:
                factor_part["factor_scores"] = json.loads(fs_raw)
            except (ValueError, TypeError):
                pass

    trace = {
        "trace_built_at": trace_built_at,
        "sources": sources,
        "model": model_part,
        "agent": {"reasoning": reasoning},
        "factors": factor_part,
    }

    # 事后 score：百分数→小数（AG-01/evaluator 统一小数）
    fwd5 = fwd10 = None
    if fwd_row is not None:
        if fwd_row["fwd_return_5d"] is not None:
            fwd5 = fwd_row["fwd_return_5d"] / 100.0
        if fwd_row["fwd_return_10d"] is not None:
            fwd10 = fwd_row["fwd_return_10d"] / 100.0

    verdict_norm = norm_verdict_fn(pred["verdict"])
    hit5 = is_hit_ag01(verdict_norm, fwd5) if fwd5 is not None else None
    hit10 = is_hit_ag01(verdict_norm, fwd10) if fwd10 is not None else None

    return {
        "prediction_id": pred["id"],
        "request_id": pred["request_id"],
        "agent_name": pred["agent_name"],
        "code": pred["code"],
        "rec_date": pred["date"],
        "verdict": pred["verdict"],
        "confidence": pred["confidence"],
        "ml_score": model_row.get("ml_score") if model_row else None,
        "factor_score": model_row.get("factor_score") if model_row else None,
        "blended_score": model_row.get("blended_score") if model_row else None,
        "model_version": model_row.get("model_version") if model_row else None,
        "reasoning": reasoning,
        "trace_json": json.dumps(trace, ensure_ascii=False, default=str),
        "fwd_return_5d": fwd5,
        "fwd_return_10d": fwd10,
        "hit_5d": None if hit5 is None else int(hit5),
        "hit_10d": None if hit10 is None else int(hit10),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="推荐回溯表回填（dry-run 先行）")
    parser.add_argument("--apply", action="store_true", help="真跑写库（默认 dry-run 零写入）")
    args = parser.parse_args()

    _load_env()
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
    from agent_layer.core.output_spec import normalize_verdict  # noqa: E402

    _emit("start", mode="apply" if args.apply else "dry_run")
    t0 = time.time()
    lock_fh = _acquire_lock() if args.apply else None

    ro = _open_ro()
    try:
        factor_cols = _factor_snapshot_columns(ro)
        preds, model, memory, factors, fwd = _load_sources(ro, factor_cols)

        existing: dict[int, dict] = {}
        if args.apply:
            from common.database import Database  # noqa: E402

            db = Database()
            db.execute(DDL)
            for stmt in INDEXES:
                db.execute(stmt)
            db.commit()
            for r in ro.execute(
                "SELECT prediction_id, fwd_return_5d, fwd_return_10d FROM recommendation_retrospect"
            ):
                existing[r["prediction_id"]] = dict(r)
        else:
            db = None

        trace_built_at = _now_cn()
        to_insert: list[dict] = []
        to_settle: list[dict] = []  # {prediction_id, 新补的 horizon 字段}
        skip_empty = 0
        cov = {"model": 0, "reasoning": 0, "factors": 0, "fwd5": 0, "fwd10": 0}

        for pred in preds:
            if not pred["date"]:
                skip_empty += 1
                _emit("skip_empty_date", prediction_id=pred["id"])
                continue
            key = (pred["date"], pred["code"])
            m_row = model.get(key)
            reasoning = memory.get(key)
            f_row = factors.get(key)
            fwd_row = fwd.get(key)
            if m_row is not None:
                cov["model"] += 1
            if reasoning is not None:
                cov["reasoning"] += 1
            if f_row is not None:
                cov["factors"] += 1

            row = _build_row(pred, m_row, reasoning, f_row, fwd_row, normalize_verdict, trace_built_at)
            if row["fwd_return_5d"] is not None:
                cov["fwd5"] += 1
            if row["fwd_return_10d"] is not None:
                cov["fwd10"] += 1

            if pred["id"] not in existing:
                to_insert.append(row)
            else:
                # 增量结算：已有档案缺的 horizon、现在有了 → 补
                ex = existing[pred["id"]]
                patch: dict = {}
                if ex["fwd_return_5d"] is None and row["fwd_return_5d"] is not None:
                    patch["fwd_return_5d"] = row["fwd_return_5d"]
                    patch["hit_5d"] = row["hit_5d"]
                if ex["fwd_return_10d"] is None and row["fwd_return_10d"] is not None:
                    patch["fwd_return_10d"] = row["fwd_return_10d"]
                    patch["hit_10d"] = row["hit_10d"]
                if patch:
                    patch["prediction_id"] = pred["id"]
                    to_settle.append(patch)

        if not args.apply:
            sample = to_insert[:2]
            _emit(
                "dry_run_summary",
                total_predictions=len(preds),
                skip_empty_date=skip_empty,
                would_insert=len(to_insert),
                would_settle=len(to_settle),
                coverage=cov,
                factor_snapshot_cols=len(factor_cols),
                fwd_excluded=[c for c in factor_cols if c.startswith(FWD_PREFIX)] or "none",
            )
            for i, s in enumerate(sample):
                _emit("dry_run_sample", n=i, row=s)
            _emit(
                "done",
                mode="dry_run",
                elapsed_s=round(time.time() - t0, 2),
                note="零写库；--apply 需统筹 GO（写库红线）",
            )
            return

        # ---- apply：串行写库 ----
        ins_sql = (
            "INSERT OR IGNORE INTO recommendation_retrospect "
            "(prediction_id, request_id, agent_name, code, rec_date, verdict, confidence, "
            " ml_score, factor_score, blended_score, model_version, reasoning, trace_json, "
            " fwd_return_5d, fwd_return_10d, hit_5d, hit_10d, settled_at) "
            "VALUES (:prediction_id, :request_id, :agent_name, :code, :rec_date, :verdict, :confidence, "
            " :ml_score, :factor_score, :blended_score, :model_version, :reasoning, :trace_json, "
            " :fwd_return_5d, :fwd_return_10d, :hit_5d, :hit_10d, "
            " CASE WHEN :fwd_return_5d IS NOT NULL OR :fwd_return_10d IS NOT NULL "
            "      THEN :settled_at ELSE NULL END)"
        )
        settled_at = _now_cn()
        inserted = 0
        for row in to_insert:
            row["settled_at"] = settled_at
            cur = db.execute(ins_sql, row)
            inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        settled = 0
        for patch in to_settle:
            pid = patch.pop("prediction_id")
            sets = ", ".join(f"{k} = :{k}" for k in patch)
            db.execute(
                f"UPDATE recommendation_retrospect SET {sets}, "
                "settled_at = COALESCE(settled_at, :settled_at), updated_at = :updated_at "
                "WHERE prediction_id = :pid",
                {**patch, "settled_at": settled_at, "updated_at": settled_at, "pid": pid},
            )
            settled += 1
        db.commit()

        total = ro.execute("SELECT COUNT(*) FROM recommendation_retrospect").fetchone()[0]
        hit5 = ro.execute("SELECT COUNT(*) FROM recommendation_retrospect WHERE hit_5d IS NOT NULL").fetchone()[0]
        hit10 = ro.execute("SELECT COUNT(*) FROM recommendation_retrospect WHERE hit_10d IS NOT NULL").fetchone()[0]
        _emit(
            "apply_summary",
            inserted=inserted,
            settled=settled,
            skip_empty_date=skip_empty,
            table_total=total,
            settled_5d=hit5,
            settled_10d=hit10,
        )
        _emit("done", mode="apply", elapsed_s=round(time.time() - t0, 2))
        if db is not None:
            db.close()
    finally:
        ro.close()
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
