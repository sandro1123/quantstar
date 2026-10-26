# -*- coding: utf-8 -*-
"""REC 模块4 通电卡 (2026-09-24) — v10 迁移 + 四表 + 三链接线。

用户裁定 (09-24): 执行层 + 甲案 + 一张通电卡。
统筹三裁: ①v10 DDL 方案甲 (字面量内联 migrations.py + 对拍测试锁漂移)
②state_transitions 不建 (basis_chain JSON 已承载) ③confidence_tier 执行层
薄封装转调 (不新建 execution→backtest 跨层 import 边)。

病根 (双席互补实证): REC 持久化面「代码全并、生产零通电」——四表生产全
MISSING、两处零调用方、schema_version=9 无 v10。对照组: 模块3
recommendation_retrospect 自己建表故有行; 1a/模块4 走懒建→没人调→永不建表。

本卡五项:
1. v10 迁移: 四表入中央清单 (tracking/_advice/rationale + 增量三字段)
2. 对拍测试: 清单字面量 vs 三处权威 DDL 逐字 (漂移即 RED)
3. 接 tracker.start() 到 orchestrator receive_plan 循环
4. 接 record_rationale() 到 agent_layer hub.decide() 后 (照抄
   _persist_invalidation_warnings 先例: 落库失败不炸主链)
5. 每日建议 + 一致性守护腿 (编排项, crontab 由统筹 22:05 窗亲挂)
"""
import json
import re
import sqlite3

import pytest


# ============ 1. v10 迁移: 四表建成 ============

def test_v10_target_version_is_10():
    """v10 须在迁移清单内且 TARGET_VERSION 不低于它。
    判例「版本号断言禁写死」: 原断言 ==10 锁死版本 —— v11 入清单即假红。
    改为「v10 块在册 + TARGET_VERSION >= 10」, 后续版本追加不再绊倒此测。"""
    from execution_layer.migrations import TARGET_VERSION, MIGRATIONS
    assert any(v == 10 for v, _ in MIGRATIONS), "v10 块须在册 (建 REC 四表)"
    assert TARGET_VERSION >= 10, f"TARGET_VERSION={TARGET_VERSION} 须>=10"


def test_v10_builds_four_rec_tables():
    """v10 迁移跑完, 四表全建成 (含增量三字段)。"""
    from common.database import Database
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    conn = db.get_connection()
    ensure_schema(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("recommendation_tracking", "recommendation_tracking_advice",
              "recommendation_rationale"):
        assert t in tables, f"v10 未建表 {t} (通电卡核心: 表必须落库非懒建)"
    # state_transitions 不建 (统筹裁定, basis_chain JSON 已承载)
    assert "state_transitions" not in tables, \
        "state_transitions 不该建 (统筹裁定: basis_chain JSON 已承载迁移史)"


def test_v10_tracking_table_has_increment_columns():
    """增量三字段进 v10 建表语句 (新表无需 ALTER)。"""
    from common.database import Database
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    conn = db.get_connection()
    ensure_schema(db)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(recommendation_tracking)").fetchall()}
    for c in ("confidence_tier", "confidence_reason", "entry_basis_trace_id"):
        assert c in cols, f"recommendation_tracking 缺增量列 {c}"


def test_v10_migration_idempotent():
    """v10 两连跑幂等 (ensure_schema 可重入, 中央迁移基本要求)。"""
    from common.database import Database
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    from execution_layer.migrations import TARGET_VERSION
    r1 = ensure_schema(db)
    r2 = ensure_schema(db)
    assert r1["version_after"] == TARGET_VERSION, "首跑应到当前 TARGET_VERSION"
    assert r2["version_before"] == TARGET_VERSION, "二跑版本不应回退"
    assert r2["applied"] == [], f"二跑不该再执行 DDL: {r2['applied']}"


# ============ 2. 对拍测试: 漂移即 RED ============

def _normalize(ddl: str) -> str:
    """规范化: 去注释(行内 -- 与 块内)、压空白、统一列序比对用。"""
    ddl = re.sub(r"--[^\n]*", "", ddl)
    return re.sub(r"\s+", " ", ddl).strip().rstrip(";").strip()


def _migration_ddl_for(table: str) -> str:
    """从 MIGRATIONS 清单取 v10 里建该表的 DDL 字面量。"""
    from execution_layer.migrations import MIGRATIONS
    v10 = dict(MIGRATIONS)[10]
    for stmt in v10:
        if f"CREATE TABLE IF NOT EXISTS {table}" in stmt:
            return stmt
    raise AssertionError(f"v10 清单无 {table} 建表语句")


def test_v10_tracking_ddl_matches_authoritative():
    """对拍: 清单 tracking DDL ≡ recommendation_tracker.TRACKING_DDL (+增量列)。"""
    from execution_layer.tracking.recommendation_tracker import TRACKING_DDL
    mig = _normalize(_migration_ddl_for("recommendation_tracking"))
    auth = _normalize(TRACKING_DDL)
    # 权威 DDL 的列必须全在清单里 (清单可多增量列, 不可少/改名)
    for col in ("code", "rec_date", "rationale_id", "verdict", "confidence",
                "state", "basis_chain", "started_at", "updated_at"):
        assert col in mig, f"清单 tracking DDL 缺权威列 {col} (漂移)"
    for col in ("confidence_tier", "confidence_reason", "entry_basis_trace_id"):
        assert col in mig, f"清单缺增量列 {col}"
    assert "UNIQUE (code, rec_date)" in mig.replace("UNIQUE(code, rec_date)", "UNIQUE (code, rec_date)"), \
        "清单 tracking DDL 缺 UNIQUE(code, rec_date) 约束 (权威定义点有)"


def test_v10_advice_ddl_matches_authoritative():
    """对拍: 清单 advice DDL ≡ ADVICE_DDL。"""
    from execution_layer.tracking.recommendation_tracker import ADVICE_DDL
    mig = _normalize(_migration_ddl_for("recommendation_tracking_advice"))
    for col in ("code", "advice_date", "state", "advice", "basis",
                "evidence", "is_reversal", "created_at"):
        assert col in mig, f"清单 advice DDL 缺权威列 {col} (漂移)"
    assert "UNIQUE" in mig, "清单 advice DDL 缺 UNIQUE(code, advice_date)"


def test_v10_rationale_ddl_matches_authoritative():
    """对拍: 清单 rationale DDL ≡ agent_layer.rationale.RATIONALE_DDL。"""
    mig = _normalize(_migration_ddl_for("recommendation_rationale"))
    # 1a 契约冻结字段 (CONTRACT_FIELDS 是本表接口)
    from agent_layer.rationale import CONTRACT_FIELDS
    for col in CONTRACT_FIELDS:
        assert col in mig, f"清单 rationale DDL 缺契约字段 {col} (契约冻结, 漂移即 RED)"
    assert "PRIMARY KEY" in mig, "rationale_id 须 PRIMARY KEY"


# ============ 3. 接线: tracker.start / record_rationale ============

def test_tracker_start_reachable_from_orchestrator():
    """接线1: orchestrator 推荐落地处须调 tracker.start (零调用→有调用)。"""
    import inspect
    from ops_layer import orchestrator
    src = inspect.getsource(orchestrator)
    assert "recommendation_tracker" in src or "RecommendationTracker" in src, \
        "orchestrator 未接 RecommendationTracker (通电卡核心: 零调用→挂上)"


def test_record_rationale_wired_in_agent_layer():
    """接线2: agent_layer 决策产出后须调 record_rationale。"""
    import inspect
    from agent_layer.core import agent_layer as al
    src = inspect.getsource(al)
    assert "record_rationale" in src, \
        "agent_layer 未接 record_rationale (LLM 判断产出未落库)"


def test_degrade_does_not_break_main_chain():
    """接线2 降级语义: 落库失败不炸主链 (照抄 invalidation 先例)。"""
    import inspect
    from agent_layer.core import agent_layer as al
    src = inspect.getsource(al)
    # 须有 try/except 包裹 + warning 降级 (同 _persist_invalidation_warnings 形态)
    m = re.search(r"record_rationale[^\n]*\n(?:.*\n){0,12}?.*except", src)
    assert m, "record_rationale 调用未包 try/except (落库失败会炸辩论主链)"


def test_confidence_tier_thin_wrapper_exists():
    """接线增量: 执行层薄封装转调 confidence_tier (不新建跨层 import 边)。"""
    try:
        from execution_layer.tracking.confidence import confidence_tier_for_tracking
    except ModuleNotFoundError as e:
        raise AssertionError(f"执行层薄封装未建: {e}") from e
    # 转调须与权威函数同语义
    from backtest_layer.evaluation.agent_eval import confidence_tier
    for c in (None, 0.1, 0.5, 0.7):
        assert confidence_tier_for_tracking(c) == confidence_tier(c), \
            f"薄封装与权威语义不一致 @{c}"


# ============ 4. 一致性守护: silent_flip 显形 (回归锚) ============

def test_silent_flip_guard_still_works_after_wiring():
    """接线后一致性守护仍工作 (回归锚: 通电不改语义)。"""
    from common.database import Database
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    from execution_layer.migrations import ensure_schema
    db = Database(":memory:")
    ensure_schema(db)
    t = RecommendationTracker(db)
    t.start(code="600001", rec_date="2026-09-24", rationale_id="r1",
            verdict="推荐", reason="量价齐升")
    rep = t.consistency_check(code="600001", asof="2026-09-25",
                              still_valid=False, invalidation_reason="跌破9.5")
    assert rep["silent_flip"] is True, "无声翻脸必须仍被探出 (通电不改守卫语义)"
