# -*- coding: utf-8 -*-
"""#112 recommendation_retrospect 结算通电 — RED 钉测.

三零病征断言 (建了没通电形态, 施工前应红):
  1. dispatch 表零 retrospect 条目
  2. ops.yaml 零槽
  3. 结算语义零实现 (新档案 INSERT + 存量 UPDATE only-NULL)

GREEN 后: 派发表有条目 + 槽位 depends_on fwd_return_backfill + handler
幂等实跑 (隔离库) inserted/settled 双口径计数。
"""
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SRC = os.path.join(ROOT, "src")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_dispatch_has_retrospect_settlement_entry():
    """零调度病征一: dispatch 表必须有 recommendation_retrospect 结算 handler 条目."""
    text = _read(os.path.join(SRC, "ops_layer", "orchestrator.py"))
    assert '"retrospect_settlement": self._run_retrospect_settlement' in text, (
        "dispatch 表缺 retrospect_settlement 条目 — 建了没通电 (crontab/ops.yaml/dispatch 三零)"
    )


def test_ops_yaml_has_retrospect_slot():
    """零调度病征二: ops.yaml 必须有 retrospect_settlement 槽位且依赖 fwd_return_backfill."""
    with open(os.path.join(ROOT, "config", "defaults", "ops.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # ops.yaml 嵌套: pipelines 在 ops: 顶层键下 (orchestrator 读 ops.pipelines)
    pipelines = (cfg.get("ops", {}) or {}).get("pipelines") or {}
    assert "retrospect_settlement" in pipelines, (
        "ops.yaml 零 retrospect_settlement 槽 — 结算写者未接线"
    )
    slot = pipelines["retrospect_settlement"]
    deps = slot.get("depends_on") or []
    assert "fwd_return_backfill" in deps, (
        "retrospect_settlement 必须挂 fwd_return_backfill 下游 (fwd 数据先结, 结算后至)"
    )


def test_backfill_no_duplicate_hit_copy():
    """canonical 收敛: backfill 脚本不得保留 is_hit_ag01 第二副本定义."""
    text = _read(os.path.join(ROOT, "scripts", "backfill_recommendation_retrospect.py"))
    assert "def is_hit_ag01" not in text, (
        "is_hit_ag01 第二副本仍在 — 须 import canonical (agent_layer.quality.evaluator 口径)"
    )


def test_settlement_core_importable_and_idempotent(tmp_path):
    """GREEN 主断言: 结算核心函数可 import, 隔离库上幂等双跑零副作用.

    语义 (与 backfill 脚本一致):
      - INSERT OR IGNORE 新档案 (prediction_id 主键)
      - UPDATE 只补 fwd IS NULL 行的缺失 horizon
      - 跳过空 date 脏行 (留痕)
      - hit 口径 = AG-01 canonical (evaluator.py), 非本地副本
    """
    sys.path.insert(0, SRC)
    from report_layer.retrospect_settlement import settle_retrospect  # noqa: E402

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    # 建最小三源: agent_predictions / factor_daily
    conn.executescript(
        """
        CREATE TABLE agent_predictions (
            id INTEGER PRIMARY KEY, request_id TEXT, agent_name TEXT,
            code TEXT, date TEXT, verdict TEXT, confidence REAL);
        CREATE TABLE factor_daily (
            date TEXT, code TEXT, fwd_return_5d REAL, fwd_return_10d REAL);
        INSERT INTO agent_predictions VALUES
            (1, 'r1', 'quick_scan', '600000', '2026-08-01', '推荐', 0.8),
            (2, 'r2', 'normal_analysis', '600001', '2026-08-01', '不推荐', 0.6),
            (3, 'r3', 'quick_scan', '600002', '', '观望', 0.3);
        INSERT INTO factor_daily VALUES
            ('2026-08-01', '600000', 2.0, NULL),
            ('2026-08-01', '600001', -1.0, NULL);
        """
    )
    conn.commit()

    stats = settle_retrospect(conn)
    # 双口径: 新档案 INSERT 2 (跳过空 date 1) + 存量结算 UPDATE (首跑表空, 全走 INSERT)
    assert stats["inserted"] == 2, f"inserted 应=2 (含空date跳过1), 实得 {stats}"
    assert stats["settled"] == 0, f"首跑存量面应=0, 实得 {stats}"
    rows = conn.execute(
        "SELECT prediction_id, fwd_return_5d, hit_5d, settled_at FROM recommendation_retrospect"
    ).fetchall()
    assert len(rows) == 2
    by_pid = {r["prediction_id"]: r for r in rows}
    # 百分数→小数 + AG-01: 推荐+上涨→hit=1; 不推荐+下跌→hit=1
    assert by_pid[1]["fwd_return_5d"] == 0.02
    assert by_pid[1]["hit_5d"] == 1
    assert by_pid[2]["hit_5d"] == 1
    assert by_pid[1]["settled_at"] is not None

    # ---- 幂等二跑: 零新增零重结 (同数据) ----
    stats2 = settle_retrospect(conn)
    assert stats2["inserted"] == 0, f"二跑应零 INSERT, 实得 {stats2}"
    assert stats2["settled"] == 0, f"二跑应零重结 (fwd 已非 NULL), 实得 {stats2}"
    n = conn.execute("SELECT COUNT(*) FROM recommendation_retrospect").fetchone()[0]
    assert n == 2, f"二跑行数应守恒=2, 实得 {n}"

    # ---- 10d 后至场景: fwd_return_10d 后补 → 存量结算 UPDATE 通道 ----
    conn.execute("UPDATE factor_daily SET fwd_return_10d = -3.0 WHERE code = '600000'")
    conn.commit()
    stats3 = settle_retrospect(conn)
    assert stats3["inserted"] == 0, f"三跑应零 INSERT, 实得 {stats3}"
    assert stats3["settled"] == 1, f"10d 后至应结算 1 行, 实得 {stats3}"
    r = conn.execute(
        "SELECT fwd_return_10d, hit_10d FROM recommendation_retrospect WHERE prediction_id = 1"
    ).fetchone()
    assert r["fwd_return_10d"] == -0.03
    assert r["hit_10d"] == 0, "推荐+10d 下跌 → miss (AG-01)"

    # ---- 新预测后至场景: INSERT 通道再开 ----
    conn.execute(
        "INSERT INTO agent_predictions VALUES (4, 'r4', 'quick_scan', '600003', '2026-08-02', '观望', 0.5)"
    )
    conn.execute(
        "INSERT INTO factor_daily VALUES ('2026-08-02', '600003', 0.5, 0.5)"
    )
    conn.commit()
    stats4 = settle_retrospect(conn)
    assert stats4["inserted"] == 1, f"四跑应 INSERT 1 新档案, 实得 {stats4}"
    # 观望 + |0.005| < 2% → hit=1 (AG-01 canonical)
    r = conn.execute(
        "SELECT hit_5d FROM recommendation_retrospect WHERE prediction_id = 4"
    ).fetchone()
    assert r["hit_5d"] == 1, "观望+微幅 → 命中 (AG-01: |actual|<2%)"

    # ---- R-1 (铁律#12 降级可观测): 最小 schema 缺 model_predictions/agent_memory
    # ---- 降级计数应 >=1, 与「当日真空」(inserted=0) 形态可区分 ----
    assert stats["degraded_sources"] >= 1, (
        f"缺源降级应计数>=1, 实得 {stats}"
    )

    conn.close()


def test_degraded_counts_missing_factor_daily():
    """R-1 边界裁定 (统筹 sm-865716cf / sm-9a970178): factor_daily 整表缺失必计数.

    #96 常驻锁先例: 新增分支不能只靠一次性隔离库实跑覆盖 —
    最小库只建 agent_predictions (无 factor_daily / model_predictions / agent_memory),
    factor_cols 空分支应 warning + _degraded 计入 "factor_daily";
    PRAGMA 空返回不抛异常不构成免责 (铁律#12 静默降级即病).
    """
    sys.path.insert(0, SRC)
    import report_layer.retrospect_settlement as retro  # noqa: E402

    import sqlite3

    conn = sqlite3.connect(":memory:")
    # 只建主源 — 三源缺三 (无 factor_daily → 触发 factor_cols 空分支)
    conn.executescript(
        """
        CREATE TABLE agent_predictions (
            id INTEGER PRIMARY KEY, request_id TEXT, agent_name TEXT,
            code TEXT, date TEXT, verdict TEXT, confidence REAL);
        INSERT INTO agent_predictions VALUES
            (1, 'r1', 'quick_scan', '600000', '2026-08-01', '推荐', 0.8);
        """
    )
    conn.commit()

    stats = retro.settle_retrospect(conn)
    # 档案照建 (trace 只有 agent 源), 因子面整体降级不硬崩
    assert stats["inserted"] == 1, f"无 factor_daily 也应建档 1 行, 实得 {stats}"
    assert "factor_daily" in retro._degraded, (
        f"factor_daily 整表缺必须计入降级 (不走静默), 实得 {stats} / {retro._degraded}"
    )
    # 三源全缺形态: model JOIN + agent_memory JOIN + factor_cols 空分支
    assert stats["degraded_sources"] >= 3, f"三缺形态计数应>=3, 实得 {stats}"

    conn.close()
