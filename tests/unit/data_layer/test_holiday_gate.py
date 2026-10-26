# -*- coding: utf-8 -*-
"""QS 卡 holiday-early-exit-gate — TDD 三钉 (红先行, Day2 2026-10-07) v2。

v2 修正: T1 mock 面改 common.utils.bj_date (gate 内部 from common.utils
import bj_date 是函数内 import, patch common.utils — 与 Day1 health 卡
同款 mock 面判例「monkeypatch 打包级属性」)。
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

_HOLIDAY = "2026-10-04"   # 国庆 (TradingCalendar 实测 False) — 审计锚
_TRADEDAY = "2026-09-30"  # 真交易日 + kline 5572 行


@pytest.fixture()
def gate_db():
    """假日 gate 测试库: daily_klines 09-30 行 (日历源)。"""
    from common.database import Database
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT NOT NULL, date TEXT NOT NULL,
            close REAL, PRIMARY KEY (code, date))
    """)
    for i in range(20):
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES (?,?,?)",
            (f"60050{i}", _TRADEDAY, 100.0))
    conn.commit()
    yield db
    conn.close()
    os.unlink(path)


def _run_on(db, date, name="kline"):
    """run_pipeline_holiday_gate 入口 (T1 面) — mock common.utils.bj_date。"""
    from data_layer import DataLayer
    from data_layer.pipelines.base import PipelineInput
    import common.utils as _utils
    dl = DataLayer.__new__(DataLayer)
    dl.db = db
    inp = PipelineInput(date=date, db=db)
    with mock.patch.object(_utils, "bj_date", return_value=date):
        return dl.run_pipeline_holiday_gate(name, inp)


def test_t1_holiday_skipped_with_visible_reason(gate_db):
    """T1: 假日 → skipped + reason 含 'non-trading day' + 日期, 零业务执行。"""
    result = _run_on(gate_db, _HOLIDAY)
    assert result is not None
    assert isinstance(result, dict) and result.get("status") == "skipped", \
        f"假日须 skipped: {result}"
    msg = str(result.get("error_msg", ""))
    assert "non-trading day" in msg, f"假日 skip 须可见痕迹 (判例 #147 反面): {msg}"
    assert _HOLIDAY in msg, f"reason 须含当日日期: {msg}"
    assert result.get("holiday_gate") is True, "gate 标记缺失"


def test_t2_trading_day_unchanged(gate_db):
    """T2: 交易日 → 原路径照跑 (gate 判定面不拦截)。"""
    from data_layer.holiday_gate import should_holiday_skip
    assert should_holiday_skip(gate_db, "kline", _TRADEDAY) is False


def test_t3_exempt_pipeline_runs_on_holiday(gate_db):
    """T3: 白名单管道 (db_backup) 假日不早退 (代码侧默认名单面)。"""
    from data_layer.holiday_gate import should_holiday_skip
    assert should_holiday_skip(gate_db, "db_backup", _HOLIDAY) is False


def test_t3b_gate_helper_holiday_true(gate_db):
    """T1 判定面: 非白名单管道假日 → True (早退)。"""
    from data_layer.holiday_gate import should_holiday_skip
    assert should_holiday_skip(gate_db, "kline", _HOLIDAY) is True


def test_t3c_calendar_fail_open(gate_db):
    """日历故障 fail-open 带痕 (判例 #147 第三形态): 故障时放行不拦。"""
    from data_layer import holiday_gate
    with mock.patch.object(
            holiday_gate.TradingCalendar, "is_trading_day",
            side_effect=RuntimeError("calendar boom")):
        assert holiday_gate.should_holiday_skip(gate_db, "kline", _HOLIDAY) is False


def test_t3d_exempt_config_consumed(gate_db):
    """T3 声明式面: ops.yaml 白名单被消费 (get_config 通路面 mock)。"""
    from data_layer import holiday_gate
    with mock.patch.object(
            holiday_gate, "_exempt_from_config",
            side_effect=lambda n: n == "marimo_daily"):
        # 白名单内 → 不早退
        assert holiday_gate.should_holiday_skip(gate_db, "marimo_daily", _HOLIDAY) is False
        # 白名单外 → 早退
        assert holiday_gate.should_holiday_skip(gate_db, "kline", _HOLIDAY) is True
