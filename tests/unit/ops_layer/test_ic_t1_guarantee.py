"""#7 动态权重 T-1 保证 — 今日 IC 不得影响今日权重。

Issue #7: 「当日权重只依赖 T-1 及以前 IC，排除 target_date 自身的 IC
（record_ic 写入同日值，若直接进调权即 T+0 看前瞻）」

编排 (测试1): ops ic_feedback handler 真实路径 —
  1. 直插 factor_ic: T-2、T-1 历史 IC + target_date 当日 IC (3 期);
  2. pre-seed factor_weights 现权重;
  3. 跑 _run_ic_feedback 真 handler (现已改为 `wp.ic_history_before(target_date)`);
  4. 断言生效权重未被当日异常高 IC 拉高 (T-1 口径)。

反向钉 (测试2): ic_history_before 严格早于 as_of_date，不含同日（这是 #7
 修复选的安全口径）。
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger
from strategy_layer.feedback.persistence import WeightPersistence


def _mk_orch():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT, "
        "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_10d REAL, "
        "fwd_return_20d REAL, PRIMARY KEY (date, code))")
    conn.execute(
        "CREATE TABLE factor_weights (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "weight_date TEXT NOT NULL, factor_name TEXT NOT NULL, "
        "weight REAL NOT NULL, method TEXT, old_weight REAL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(weight_date, factor_name))")
    conn.execute(
        "CREATE TABLE factor_ic (calc_date TEXT NOT NULL, "
        "factor_name TEXT NOT NULL, mean_ic REAL, std_ic REAL, icir REAL, "
        "win_rate REAL, effectiveness TEXT, sample_size INTEGER, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (calc_date, factor_name))")
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _seed_factor_daily(db, date, fwd_col):
    conn = db.get_connection()
    for i in range(12):
        code = "%06d" % i
        conn.execute(
            f"INSERT INTO factor_daily (date, code, factor_scores, {fwd_col}) "
            "VALUES (?, ?, ?, ?)",
            (date, code, json.dumps({"roe_quality": float(i)}), float(i) * 0.1))
    conn.commit()


def _seed_factor_ic(db, factor, calc_date, ic):
    conn = db.get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO factor_ic "
        "(calc_date, factor_name, mean_ic, std_ic, icir, win_rate, "
        "effectiveness, sample_size, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (calc_date, factor, ic, 0.02, 0.4, 0.6, "strong", 12, calc_date))
    conn.commit()


def _seed_weights(db, weights):
    conn = db.get_connection()
    for fname, w in weights.items():
        conn.execute(
            "INSERT OR REPLACE INTO factor_weights "
            "(weight_date, factor_name, weight, method) VALUES (?,?,?,?)",
            ("2026-08-18", fname, w, "ic_decay"))
    conn.commit()


def _run_feedback(orch):
    return orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})


def _read_latest_weights(db):
    conn = db.get_connection()
    row = conn.execute("SELECT MAX(weight_date) FROM factor_weights").fetchone()
    latest = row[0] if row else None
    if not latest:
        return None
    rows = conn.execute(
        "SELECT factor_name, weight FROM factor_weights WHERE weight_date = ?",
        (latest,)).fetchall()
    return {r[0]: r[1] for r in rows}


class TestT1EndToEnd:
    """handler 真路径：当日 IC 不得拉高今日权重。"""

    def test_t1_guarantee_current_day_ic_excluded(self):
        db, orch = _mk_orch()
        today = "2026-08-25"      # target_date（handler 的滞后解析日）
        t_minus_1 = "2026-08-22"  # 前一交易日（上周五）
        t_minus_2 = "2026-08-21"  # 再前一日（上周四）

        # 3 期 IC：T-2=0.10, T-1=0.20, T-0(当日)=0.90 (异常高)
        _seed_factor_ic(db, "roe_quality", t_minus_2, 0.10)
        _seed_factor_ic(db, "roe_quality", t_minus_1, 0.20)
        _seed_factor_ic(db, "roe_quality", today, 0.90)

        # pre-seed 现权重
        _seed_weights(db, {"roe_quality": 0.5, "other_factor": 0.5})

        # factor_daily 铺到 target_date 供 handler 读取
        _seed_factor_daily(db, today, "fwd_return_10d")

        result = _run_feedback(orch)
        assert result.success is True, result.metadata

        weights = _read_latest_weights(db)
        assert weights is not None, "应当写入今日权重"

        # T-1 口径下 roe_quality 权重只依据 0.10/0.20，不会被当日 0.90 拉高。
        # 阈值 0.6：T-1 口径远低于此；若发生 T+0 看前瞻则会显著更高。
        assert weights.get("roe_quality", 0) < 0.6, (
            f"T+0 看前瞻回归: roe_quality 权重={weights.get('roe_quality'):.4f} "
            f"疑似把当日 IC(0.90) 计入今日调权，应只用 T-1 及以前 IC")

        # 当日 IC 已落库供观测，但不进入调权
        assert db.get_connection().execute(
            "SELECT COUNT(*) FROM factor_ic WHERE calc_date = ?", (today,)).fetchone()[0] == 1


class TestIcHistoryBeforeUnit:
    """ic_history_before 纯函数：严格早于 as_of_date，不含同日。"""

    def test_excludes_as_of_date(self):
        db = Database(":memory:")
        db.init_db()
        conn = db.get_connection()
        conn.executescript(
            "CREATE TABLE factor_ic (calc_date TEXT NOT NULL, "
            "factor_name TEXT NOT NULL, mean_ic REAL, PRIMARY KEY "
            "(calc_date, factor_name))")
        # 3 期（近期日期，在 date('now','-30 days') UTC 窗口内）
        for d, ic in [("2026-10-27", 0.10), ("2026-10-28", 0.20),
                      ("2026-10-29", 0.90)]:
            conn.execute(
                "INSERT INTO factor_ic (calc_date, factor_name, mean_ic) "
                "VALUES (?,?,?)", (d, "roe_quality", ic))
        conn.commit()
        wp = WeightPersistence(db=db)
        conn = db.get_connection()
        wp._load_ic_history(conn)
        today = "2026-10-29"        # T-0(当日)
        before = wp.ic_history_before(today)["roe_quality"]
        # 严格小于 today: 0.10 (10-27) + 0.20 (10-28)，不含 0.90 (10-29 当日)
        assert set(before) == {0.10, 0.20}, (
            f"ic_history_before(2026-10-29) 应只含 T-2/T-1，实际 {before}")
        assert 0.90 not in before, "同日值 0.90 泄漏进 T-1 窗口"

    def test_returns_empty_when_no_db(self):
        wp = WeightPersistence(db=None)
        assert wp.ic_history_before("2026-10-29") == {}
