# -*- coding: utf-8 -*-
"""TDD RED: 卡组二修法刀1+刀3 (派单 sm-33828e0a16304c5a, main=4db15b5f)

刀1 (freshness 假日守卫): check_collect_freshness 对非交易日 audit 日,
交易日工件类表 (limit_break_pool/stock_anomaly_reasons/auction_*) 不判
「当日 missing」—— 假日无涨停炸板/异动/竞价数据是天然形态, 不是断供。
判据 = TradingCalendar (静态 holiday 表, 不读 daily_klines — 库停更=病源,
拿它当锚=用有病产物当基线, 同 prev_trading_day_before v4 注释)。
自然日供数类表 (hot_stock_history/skyrocket_list/limit_up_ladder/
sector_fund_flow) 假日照判 — fuyao 按自然日供数, 10-02 实测有行。
「假日缺」不是「无信息」: missing→skipped(holiday) 显形, 哨兵不哑。

刀3 (best_effort_degraded 不再被 kline_fail 分支丢弃):
_run_data_pipeline 收官 overall_errors 非空走 kline_fail 分支提前 return,
best_effort_degraded 清单整体丢失 — 10-09 生产实证: DDP error_msg 无
shortterm 段 (partial 全貌只在 quantstar.log:26739)。三个 return 点
(partial success / failed / 失败) 都必须把 best_effort_degraded 并入
errors/metadata.errors。
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database


# ===========================================================================
# 刀1: 假日守卫
# ===========================================================================

TRADING_DAY_ARTIFACTS = (
    "limit_break_pool", "stock_anomaly_reasons",
    "auction_benchmark", "auction_snapshot",
)
NATURAL_DAY_SOURCES = (
    "hot_stock_history", "skyrocket_list",
    "limit_up_ladder", "sector_fund_flow",
)


def _readiness_db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS limit_break_pool (
            code TEXT, date TEXT, name TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS stock_anomaly_reasons (
            code TEXT, date TEXT, tag_name TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS auction_benchmark (
            code TEXT, date TEXT, auction_pct REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS auction_snapshot (
            code TEXT, date TEXT, auction_price REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS hot_stock_history (
            code TEXT, date TEXT, rank INTEGER, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS skyrocket_list (
            code TEXT, date TEXT, rank INTEGER, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS limit_up_ladder (
            code TEXT, snapshot_date TEXT, board_level TEXT, PRIMARY KEY(code, snapshot_date, board_level));
        CREATE TABLE IF NOT EXISTS sector_fund_flow (
            name TEXT, date TEXT, sector_type TEXT, PRIMARY KEY(name, date));
    """)
    conn.commit()
    return db, conn


def _holiday_date() -> str:
    """日历里的国庆假日 (TradingCalendar._holidays 实测 10-01~10-08)。"""
    return "2026-10-02"


def _trading_date() -> str:
    """节后第一个交易日。"""
    return "2026-10-09"


class TestHolidayGuardRegistry:
    def test_artifact_registry_exists_with_expected_members(self):
        """交易日工件类注册表必须存在且四表在册。"""
        from data_layer.health.readiness import DataReadiness
        reg = getattr(DataReadiness, "TRADING_DAY_ARTIFACT_TABLES", None)
        assert reg is not None, \
            "缺 TRADING_DAY_ARTIFACT_TABLES 注册表 (假日守卫无载体)"
        for t in TRADING_DAY_ARTIFACTS:
            assert t in reg, f"{t} 是交易日工件 (炸板/异动/竞价) 必须入册"

    def test_registry_members_not_overlapping_natural_day(self):
        """两注册表不得交叠 — 同表两语义=判定漂移。"""
        from data_layer.health.readiness import DataReadiness
        reg = getattr(DataReadiness, "TRADING_DAY_ARTIFACT_TABLES", None)
        assert reg is not None, "注册表必须先存在才谈交叠"
        for t in NATURAL_DAY_SOURCES:
            assert t not in reg, \
                f"{t} 是自然日供数类 (假日有行) 不得入工件册"


class TestHolidayGuardBehavior:
    def test_holiday_artifact_tables_not_missing(self):
        """假日审计: 四张工件表当日无行 → 不得判 missing (假报根除)。

        10-02 实测: break/anomaly/auction 假日无数据=天然形态, 旧行为
        每日假告警 (audit 逐日史 10-02/05/06/07 四条假 missing)。
        """
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        r = DataReadiness(db).check_collect_freshness(_holiday_date())
        for t in TRADING_DAY_ARTIFACTS:
            info = r.get(t, {})
            assert info.get("fresh") is not False, (
                f"假日 {t} 无行必须跳过 missing 判定 (假报); 实得 {info}")
            assert info.get("skipped") == "holiday", (
                f"假日 {t} 必须显式标 skipped=holiday (哨兵不哑); 实得 {info}")

    def test_holiday_natural_day_tables_still_checked(self):
        """假日审计: 自然日供数类表仍查当日 (fuyao 假日供数, 10-02 有行)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        hol = _holiday_date()
        conn.execute(
            "INSERT INTO hot_stock_history (code, date, rank) VALUES ('600519', ?, 1)", (hol,))
        conn.execute(
            "INSERT INTO skyrocket_list (code, date, rank) VALUES ('000001', ?, 1)", (hol,))
        conn.commit()
        r = DataReadiness(db).check_collect_freshness(hol)
        assert r["hot_stock_history"]["fresh"] is True, \
            "假日自然日源有行必须判 fresh (10-02 实测 30 行)"
        assert "skipped" not in r["hot_stock_history"], \
            "自然日供数类假日不跳过"
        assert r["skyrocket_list"]["fresh"] is True

    def test_holiday_natural_day_tables_missing_is_real(self):
        """假日自然日源无行 → 仍判 missing (真断供显形, 守卫不扩大范围)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        r = DataReadiness(db).check_collect_freshness(_holiday_date())
        assert r["hot_stock_history"]["fresh"] is False, \
            "假日 hot 无行仍是真 missing — 守卫只豁免工件类"

    def test_trading_day_artifacts_still_checked_same_day(self):
        """交易日审计: 工件表仍查当日 — 修法不误伤真红 (10-08 实证)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        td = _trading_date()
        conn.execute(
            "INSERT INTO limit_break_pool (code, date, name) VALUES ('600519', ?, 'x')", (td,))
        conn.commit()
        r = DataReadiness(db).check_collect_freshness(td)
        assert r["limit_break_pool"]["fresh"] is True, \
            "交易日 break 有行必须 fresh"
        assert "skipped" not in r["limit_break_pool"], \
            "交易日不跳过判定"

    def test_trading_day_artifact_empty_is_real_missing(self):
        """交易日工件表无行 → 真 missing (10-08 五表真缺, 不得被守卫吞)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        r = DataReadiness(db).check_collect_freshness(_trading_date())
        for t in TRADING_DAY_ARTIFACTS:
            assert r[t]["fresh"] is False, \
                f"交易日 {t} 无行必须判 missing — 守卫只豁免假日"

    def test_unknown_calendar_date_not_skipped(self):
        """日历判不了的日期 (未来/坏格式) → 不跳过, 保守判当日 (哨兵不哑)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        r = DataReadiness(db).check_collect_freshness("2099-01-01")
        assert "skipped" not in r.get("limit_break_pool", {}), \
            "未来日不得凭假日守卫跳过 (日历不覆盖=不豁免)"

    def test_t1_tables_unaffected_by_holiday_guard(self):
        """T+1 源 (index_daily/lhb_daily) 逻辑不挪窝 — 守卫只碰当日清单。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS index_daily (
                code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
            CREATE TABLE IF NOT EXISTS lhb_daily (
                code TEXT, date TEXT, net_amount REAL, PRIMARY KEY(code, date));
        """)
        conn.commit()
        r = DataReadiness(db).check_collect_freshness(_trading_date())
        assert "index_daily" in r and "lhb_daily" in r, "T1 表仍在结果里"


# ===========================================================================
# 刀3: kline_fail 分支不得丢弃 best_effort_degraded
# ===========================================================================

def _minimal_app_stub():
    """最小 app stub: _run_data_pipeline 只碰 data.run_pipeline/db。"""
    from types import SimpleNamespace
    return SimpleNamespace(data=SimpleNamespace(db=None, pipelines={}, run_pipeline=None))


class TestBestEffortDegradedSurvivesKlineFailBranch:
    """10-09 生产实证: overall_errors 非空 (kline partial 920305) →
    kline_fail 分支提前 return, best_effort_degraded 整体丢失,
    DDP error_msg 无 shortterm 段 (全貌只在 quantstar.log:26739)。

    修法: 三个 return 点 (partial success / failed / 尾部) 都必须把
    best_effort_degraded 并入 errors/metadata.errors。
    """

    def _run_ddp_with(self, monkeypatch, *, kline_ok: bool, degraded: list[str],
                      kline_rows: int = 1):
        """跑 _run_data_pipeline: 子管道结果按 kline_ok 注入, 返回 HandlerResult。

        kline_rows 控制 daily_klines 探针行数: >=MIN_KLINE_ROWS 走
        partial-success 支; <MIN_KLINE_ROWS 且 >0 保持 failed; 0 也 failed。
        """
        from ops_layer.orchestrator import PipelineOrchestrator
        from ops_layer.core.models import HandlerResult
        from data_layer.pipelines.base import PipelineInput
        from types import SimpleNamespace

        orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
        app = _minimal_app_stub()
        app.data.pipelines = {"shortterm_collect": object()}  # 条件腿已注册
        app.data.run_pipeline = lambda name, inp: HandlerResult(
            success=kline_ok if name == "kline" else True,
            records_written=10,
            errors=[] if (kline_ok or name != "kline") else ["kline boom"],
            metadata={"partial": False},
        )
        # daily_klines 覆盖探针: kline_rows >= MIN_KLINE_ROWS 走 partial-success 支
        monkeypatch.setattr(
            "ops_layer.orchestrator.MIN_KLINE_ROWS", 1, raising=False)
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
        for i in range(kline_rows):
            conn.execute(
                "INSERT INTO daily_klines VALUES (?, '2026-10-19', 10.0)",
                (f"60000{i}",))
        conn.commit()
        app.data.db = SimpleNamespace(
            db_path=":memory:", get_connection=lambda: conn)

        cfg = {"start_date": "2026-10-19", "end_date": "2026-10-19"}
        with patch.object(PipelineOrchestrator, "_get_app", lambda self: app), \
             patch.object(PipelineOrchestrator, "_min_kline_rows", lambda self: 1):
            # 拦截 best_effort_degraded 累积: 经 degraded 参数指定内容 —
            # 直接塞进 orchestration 的常见路径不可行, 改为让 shortterm_collect
            # 报 partial 触发 best_effort 降级记录。
            orig = app.data.run_pipeline

            def _run(name, inp):
                if name == "shortterm_collect":
                    return HandlerResult(
                        success=True, records_written=30,
                        errors=[d for d in degraded],
                        metadata={"partial": True})
                return orig(name, inp)

            app.data.run_pipeline = _run
            result = orch._run_data_pipeline(cfg)
        return result

    def test_partial_success_branch_keeps_degraded(self, monkeypatch):
        """kline_fail 且 kline_rows 充足 → partial success 支, degraded 必须可见。"""
        degraded = ["shortterm_collect: partial (limit_ladder: 无数据)"]
        result = self._run_ddp_with(monkeypatch, kline_ok=False, degraded=degraded)
        errs = list(getattr(result, "errors", []) or [])
        meta = getattr(result, "metadata", {}) or {}
        meta_errs = list(meta.get("errors", []) or [])
        combined = " | ".join(str(e) for e in errs + meta_errs)
        assert "shortterm_collect" in combined, (
            f"partial success 支必须保留 best_effort_degraded; 实得 errors={errs} "
            f"metadata.errors={meta_errs}")

    def test_failed_branch_keeps_degraded(self, monkeypatch):
        """overall_errors 非空且 kline_rows 不足 → failed 支, degraded 必须可见。"""
        degraded = ["shortterm_collect: partial (anomaly: 无数据)"]
        # kline_rows=0 (< MIN_KLINE_ROWS 且不 >0) → 不降级 partial, 走 failed 支
        result = self._run_ddp_with(monkeypatch, kline_ok=False, degraded=degraded,
                                    kline_rows=0)
        errs = list(getattr(result, "errors", []) or [])
        combined = " | ".join(str(e) for e in errs)
        assert "shortterm_collect" in combined, (
            f"failed 支必须保留 best_effort_degraded; 实得 errors={errs}")


# ===========================================================================
# 反向钉 (机治钉三问第三问): 守卫不误伤 — 真数据必须 still fresh
# ===========================================================================

class TestHolidayGuardDoesNotSwallowRealData:
    """机治钉三问: 它不宣称做的事会误伤吗?
    守卫只豁免「假日+工件表+0 行」三条件同时成立; 任何一边不成立都必须
    按旧语义判, 否则就是把真红洗成 skipped (假绿)。
    """

    def test_trading_day_with_rows_still_fresh(self):
        """交易日有行 → fresh (守卫不碰)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        td = _trading_date()
        conn.execute(
            "INSERT INTO limit_break_pool (code, date, name) VALUES ('600519', ?, 'x')", (td,))
        conn.execute(
            "INSERT INTO stock_anomaly_reasons (code, date, tag_name) VALUES ('000001', ?, 't')", (td,))
        conn.commit()
        r = DataReadiness(db).check_collect_freshness(td)
        assert r["limit_break_pool"]["fresh"] is True
        assert r["stock_anomaly_reasons"]["fresh"] is True
        assert "skipped" not in r["limit_break_pool"]

    def test_holiday_with_rows_still_fresh(self):
        """假日工件表有行 (源主动供数) → fresh, 不得因假日标 skipped。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        hol = _holiday_date()
        conn.execute(
            "INSERT INTO limit_break_pool (code, date, name) VALUES ('600519', ?, 'x')", (hol,))
        conn.commit()
        r = DataReadiness(db).check_collect_freshness(hol)
        assert r["limit_break_pool"]["fresh"] is True, (
            "假日有行必须 fresh — 守卫只豁免 0 行, 有行洗 skipped=假绿")
        assert "skipped" not in r["limit_break_pool"]

    def test_trading_day_zero_rows_real_missing(self):
        """交易日 0 行 → missing (10-08 实证五表真缺, 守卫不许吞)。"""
        from data_layer.health.readiness import DataReadiness
        db, conn = _readiness_db()
        r = DataReadiness(db).check_collect_freshness(_trading_date())
        assert r["limit_break_pool"]["fresh"] is False
        assert "skipped" not in r["limit_break_pool"], (
            "交易日 0 行不得标 skipped — 那是真断供 (10-08 实证)")
