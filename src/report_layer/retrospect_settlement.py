# -*- coding: utf-8 -*-
"""recommendation_retrospect 结算核心 (#112 通电).

设计承自 scripts/backfill_recommendation_retrospect.py (P0-1, 6f0bde2):
  - trace 一次性建档不可变 (推荐当时的快照)
  - score/hit 增量结算 (只补缺失 horizon, UPDATE 仅 fwd IS NULL 行)
  - 幂等: prediction_id 主键 INSERT OR IGNORE; 重跑零副作用
  - hit 口径 = AG-01 canonical: agent_layer.quality.evaluator.is_hit_ag01
    唯一实现点 (#112 收敛) — 本模块与 backfill 脚本均 import 之, 零本地副本
  - 空 date 脏行跳过 (voided_legacy 形态), 不建档案不结算
  - 三源 (model_predictions/agent_memory/factor_daily) 缺表缺列诚实降级
    不硬崩 — 隔离测试最小 schema 与生产列演化两态皆可跑
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

# AG-01 canonical 唯一实现点 (#112 收敛): hit 判定与 verdict 归一均 import 自
# agent_layer (evaluator / output_spec), 本模块零本地副本 — 手抄副本史终结。
from agent_layer.core.output_spec import normalize_verdict
from agent_layer.quality.evaluator import is_hit_ag01

# R-1 (铁律#12 降级可观测): 降级必留痕
from common.logging import get_logger

logger = get_logger("report.retrospect_settlement")

_TZ_CN = timezone(timedelta(hours=8))

# factor_daily 未来数据列前缀 — trace 快照必须排除 (防泄漏, PIT 铁律)
FWD_PREFIX = "fwd_return_"
FACTOR_KEY_COLS = {"date", "code"}
FWD_HORIZON_COLS = ("fwd_return_5d", "fwd_return_10d")

# 本次结算降级源留痕 (R-1): settle 入口清零, stats 出口 len()
_degraded = []

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
    reasoning        TEXT,
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


def _now_cn() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _try(conn, sql):
    """诚实降级执行: 源表/源列缺失返回 [] 而非硬崩 (OperationalError).

    R-1 (铁律#12): 降级必留痕 — 零留痕时源表缺失的
    inserted=0 与当日真空同形态不可区分, warning + 计数双留痕.
    """
    try:
        return list(conn.execute(sql))
    except sqlite3.OperationalError as e:
        logger.warning("[retrospect] 源表不可用降级为空: %s / %s", sql[:60], e)
        _degraded.append(sql[:60])
        return []


def _factor_snapshot_columns(conn) -> list:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(factor_daily)")]
    return [c for c in cols if c not in FACTOR_KEY_COLS and not c.startswith(FWD_PREFIX)]


def _load_sources(conn, factor_cols):
    """三路 trace 源 + 事后收益 (全部 (date, code) 1:1 键; 缺源降级空 dict)."""
    preds = [
        dict(zip(("id", "request_id", "agent_name", "code", "date", "verdict", "confidence"), r))
        for r in conn.execute(
            "SELECT id, request_id, agent_name, code, date, verdict, confidence "
            "FROM agent_predictions ORDER BY id"
        )
    ]
    model = {
        (r[0], r[1]): {"ml_score": r[2], "factor_score": r[3], "blended_score": r[4], "model_version": r[5]}
        for r in _try(conn,
            "SELECT m.date, m.code, m.ml_score, m.factor_score, m.blended_score, m.model_version "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN model_predictions m ON m.date = p.date AND m.code = p.code")
    }
    memory = {
        (r[0], r[1]): r[2]
        for r in _try(conn,
            "SELECT am.date, am.code, am.reasoning "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN agent_memory am ON am.date = p.date AND am.code = p.code")
    }
    factors = {}
    if factor_cols:
        fcols_sql = ", ".join("f." + c for c in factor_cols)
        for r in _try(conn,
            "SELECT f.date, f.code, " + fcols_sql + " "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN factor_daily f ON f.date = p.date AND f.code = p.code"):
            d = dict(zip(["date", "code"] + list(factor_cols), r))
            key = (d.pop("date"), d.pop("code"))
            factors[key] = d
    # horizon 列以 factor_daily 实有列为准 (PRAGMA 对拍, 缺列自动跳)
    fcols_all = [r[1] for r in _try(conn, "PRAGMA table_info(factor_daily)")]
    want = [c for c in FWD_HORIZON_COLS if c in fcols_all]
    fwd = {}
    if want:
        sel = ", ".join("f." + c for c in want)
        for r in _try(conn,
            "SELECT f.date, f.code, " + sel + " "
            "FROM (SELECT DISTINCT date, code FROM agent_predictions WHERE date <> '') p "
            "JOIN factor_daily f ON f.date = p.date AND f.code = p.code"):
            fwd[(r[0], r[1])] = dict(zip(want, r[2:]))
    return preds, model, memory, factors, fwd


def _build_row(pred, model_row, reasoning, factor_row, fwd_row, trace_built_at) -> dict:
    sources = ["agent_predictions"]
    model_part = None
    if model_row is not None:
        sources.append("model_predictions")
        model_part = dict(model_row)
    if reasoning is not None:
        sources.append("agent_memory")
    factor_part = None
    if factor_row is not None:
        sources.append("factor_daily")
        factor_part = dict(factor_row)
    trace = {
        "trace_built_at": trace_built_at,
        "sources": sources,
        "model": model_part,
        "agent": {"reasoning": reasoning},
        "factors": factor_part,
    }
    fwd5 = fwd10 = None
    if fwd_row is not None:
        v5 = fwd_row.get("fwd_return_5d")
        v10 = fwd_row.get("fwd_return_10d")
        if v5 is not None:
            fwd5 = v5 / 100.0  # 百分数→小数 (AG-01)
        if v10 is not None:
            fwd10 = v10 / 100.0
    verdict_norm = normalize_verdict(pred["verdict"])
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
        "ml_score": model_row["ml_score"] if model_row else None,
        "factor_score": model_row["factor_score"] if model_row else None,
        "blended_score": model_row["blended_score"] if model_row else None,
        "model_version": model_row["model_version"] if model_row else None,
        "reasoning": reasoning,
        "trace_json": json.dumps(trace, ensure_ascii=False, default=str),
        "fwd_return_5d": fwd5,
        "fwd_return_10d": fwd10,
        "hit_5d": None if hit5 is None else int(hit5),
        "hit_10d": None if hit10 is None else int(hit10),
    }


def settle_retrospect(db) -> dict:
    """结算主入口 (#112 handler 核心).

    db: Database (common.database) 或 sqlite3.Connection — 两者均有 .execute/.commit.
    返回双口径计数: inserted (新档案) / settled (存量补结) / skipped_empty_date.
    """
    _degraded.clear()  # R-1: 本次结算计数, 不跨跑累积
    db.execute(DDL)
    for stmt in INDEXES:
        db.execute(stmt)
    db.commit()

    factor_cols = _factor_snapshot_columns(db)
    if not factor_cols:
        # R-1 边界裁定 (统筹 sm-865716cf): 整表缺/无可用列也计数 —
        # PRAGMA 空返回不抛异常不构成免责 (铁律#12 静默降级即病)
        logger.warning("[retrospect] factor_daily 无列/缺表, 因子面降级")
        _degraded.append("factor_daily")
    preds, model, memory, factors, fwd = _load_sources(db, factor_cols)
    existing = {
        r[0]: {"fwd_return_5d": r[1], "fwd_return_10d": r[2]}
        for r in db.execute(
            "SELECT prediction_id, fwd_return_5d, fwd_return_10d FROM recommendation_retrospect"
        )
    }

    trace_built_at = _now_cn()
    to_insert = []
    to_settle = []
    skipped = 0
    for pred in preds:
        if not pred["date"]:
            skipped += 1
            continue
        key = (pred["date"], pred["code"])
        row = _build_row(
            pred, model.get(key), memory.get(key), factors.get(key), fwd.get(key), trace_built_at
        )
        if pred["id"] not in existing:
            to_insert.append(row)
        else:
            ex = existing[pred["id"]]
            patch = {}
            if ex["fwd_return_5d"] is None and row["fwd_return_5d"] is not None:
                patch["fwd_return_5d"] = row["fwd_return_5d"]
                patch["hit_5d"] = row["hit_5d"]
            if ex["fwd_return_10d"] is None and row["fwd_return_10d"] is not None:
                patch["fwd_return_10d"] = row["fwd_return_10d"]
                patch["hit_10d"] = row["hit_10d"]
            if patch:
                patch["prediction_id"] = pred["id"]
                to_settle.append(patch)

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
        sets = ", ".join(k + " = :" + k for k in patch)
        db.execute(
            "UPDATE recommendation_retrospect SET " + sets + ", "
            "settled_at = COALESCE(settled_at, :settled_at), updated_at = :updated_at "
            "WHERE prediction_id = :pid",
            dict(patch, settled_at=settled_at, updated_at=settled_at, pid=pid),
        )
        settled += 1
    db.commit()
    return {
        "inserted": inserted,
        "settled": settled,
        "skipped_empty_date": skipped,
        "degraded_sources": len(_degraded),  # R-1 (铁律#12)
    }
