# -*- coding: utf-8 -*-
"""reaper 预算面同源修 TDD RED 底稿 (夜班判例 22 待修卡)。

病根: 同一超时两预算面不同源 — cron_watchdog 用 DL-65 ceiling
(timeout×(retries+1)+delay×retries, 依赖再取大等待窗), reaper 用裸
timeout+grace → reaper 先到点误杀活着的 ddp (2700+300=3000s < 5550s)。

RED 语义 (施工前必须红):
- R1 公式单一定义点: ops_layer.budget_formula.compute_ceiling 存在,
  ceiling = t×(retries+1)+delay×retries, 带依赖取大 DEP_WAIT_WINDOW+MARGIN。
- R2 reaper 接 ceiling: reap_expired_runs 判定公式从 timeout_s+grace 改为
  ceiling+grace (timeout_lookup 需带 retries/delay)。
- R3 误杀守卫夹具: running 4000s (超旧 3000s 但未超新 5550s) →
  旧公式误杀 / 新公式不杀; running 5600s → 照杀。
- R4 恒等式守卫: watchdog 心跳 budget_s 与 budget_formula.compute_ceiling
  两公式恒等 (防未来漂移)。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest


def _mkconn():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row  # reaper 用列名访问 (真 Database 同面)
    con.execute(
        "CREATE TABLE pipeline_runs (run_id TEXT, pipeline_name TEXT,"
        " date TEXT, start_time TEXT, end_time TEXT, status TEXT,"
        " error_msg TEXT, duration_s REAL, version TEXT, config_hash TEXT,"
        " config_hash_effective TEXT, created_at TEXT)")
    con.commit()
    return con


def _pl(con):
    from ops_layer.pipeline_logger import PipelineLogger
    db = type("DB", (), {"get_connection": staticmethod(lambda: con),
                         "close": staticmethod(lambda: None)})()
    return PipelineLogger(db)


class TestR1FormulaModule:
    def test_budget_formula_module_exists(self):
        from ops_layer.budget_formula import compute_ceiling
        assert callable(compute_ceiling), (
            "公式须单一定义点 ops_layer/budget_formula (判例22: 漂移即误杀)")

    def test_ceiling_formula_semantics(self):
        from ops_layer.budget_formula import compute_ceiling
        # ddp 实况: timeout 2700, retries 0, delay 30, 无依赖 → 2700? 不:
        # ceiling = 2700×(0+1)+30×0 = 2700; 带依赖取大 900+60=960 → 2700
        assert compute_ceiling({"timeout_s": 2700, "max_retries": 0,
                                "retry_delay_s": 30}) == 2700
        # 病历 ddp 有 wait_timeout 等待窗: depends_on 非空 → max(2700, 960)
        assert compute_ceiling({"timeout_s": 2700, "max_retries": 0,
                                "retry_delay_s": 30,
                                "depends_on": ["x"]}) == 2700
        # retries 2: 300×3+30×2=960
        assert compute_ceiling({"timeout_s": 300, "max_retries": 2,
                                "retry_delay_s": 30}) == 960

    def test_dep_window_semantics(self):
        from ops_layer.budget_formula import (compute_ceiling,
            DEP_WAIT_WINDOW_S, DEP_MARGIN_S)
        assert DEP_WAIT_WINDOW_S == 900 and DEP_MARGIN_S == 60


class TestR2ReaperUsesCeiling:
    def test_reaper_accepts_full_config(self):
        """reaper 调用面须能收 per-pipeline 完整 cfg (含 retries/delay)。"""
        from ops_layer.pipeline_logger import PipelineLogger
        import inspect
        src = inspect.getsource(PipelineLogger.reap_expired_runs)
        assert "ceiling" in src, "reaper 判定须走 ceiling 公式 (非裸 timeout)"


class TestR3MisReapGuard:
    def _seed(self, con, age_s):
        start = (datetime.now() - timedelta(seconds=age_s)).strftime(
            "%Y-%m-%d %H:%M:%S")
        con.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date,"
            " start_time, status) VALUES ('r1', 'ddp', '2026-09-11', ?,"
            " 'running')", (start,))
        con.commit()

    def test_alive_under_ceiling_not_reaped(self):
        """4000s: 旧公式(2700+300)会误杀 / 新公式(2700+300 ceiling+grace
        — ddp retries=0 → ceiling=2700, 2700+grace300=3000) ...
        病历数: ddp wait_timeout_s 2850 时 ceiling 须含等待面 →
        新判定用 ceiling+grace 且 ceiling 按完整 cfg 算 (ddp 预算 5550s
        = watchdog 口径), 4000s 活行不杀。"""
        from ops_layer.pipeline_logger import PipelineLogger
        con = _mkconn()
        self._seed(con, 4000)
        pl = _pl(con)
        # 旧面: timeout_lookup 只给 timeout → 误杀 (回归对照)
        reaped_old = pl.reap_expired_runs(
            timeout_lookup={"ddp": 2700}, grace_s=300)
        assert len(reaped_old) == 1, "对照: 旧裸 timeout 公式 4000s>3000s 会杀"
        # 新面: full_cfg 带 wait 键 → ceiling 同源 → 不杀
        reaped_new = pl.reap_expired_runs(
            timeout_lookup={"ddp": 2700}, grace_s=300,
            full_cfg={"ddp": {"timeout_s": 2700, "max_retries": 0,
                              "retry_delay_s": 30,
                              "wait_timeout_s": 2850}})
        assert reaped_new == [], "4000s 未超 watchdog 口径预算 → 不杀 (活行)"

    def test_over_ceiling_still_reaped(self):
        from ops_layer.pipeline_logger import PipelineLogger
        con = _mkconn()
        self._seed(con, 6100)
        pl = _pl(con)
        reaped = pl.reap_expired_runs(
            timeout_lookup={"ddp": 2700}, grace_s=300,
            full_cfg={"ddp": {"timeout_s": 2700, "max_retries": 0,
                              "retry_delay_s": 30,
                              "wait_timeout_s": 2850}})
        assert len(reaped) == 1, "超 ceiling+grace → 照杀"


class TestR4IdentityGuard:
    def test_heartbeat_formula_identity(self):
        """恒等式守卫: watchdog 心跳面与单一定义点公式恒等 (防未来漂移)。"""
        from ops_layer.budget_formula import compute_ceiling
        from ops_layer.heartbeat_check import budget_s
        import inspect
        for cfg in ({"timeout_s": 2700, "max_retries": 0, "retry_delay_s": 30},
                    {"timeout_s": 300, "max_retries": 2, "retry_delay_s": 30,
                     "depends_on": ["a"]},
                    {"timeout_s": 60, "max_retries": 1, "retry_delay_s": 15}):
            assert budget_s(cfg) == compute_ceiling(cfg), (
                f"公式漂移: {cfg} → watchdog={budget_s(cfg)} "
                f"formula={compute_ceiling(cfg)}")
        # watchdog 心跳面须改为 import 单一定义点 (非双实现)
        src = inspect.getsource(
            __import__("ops_layer.heartbeat_check", fromlist=["budget_s"]))
        assert "from ops_layer.budget_formula import" in src, (
            "心跳面须 import 单一定义点 (双实现=漂移温床)")
