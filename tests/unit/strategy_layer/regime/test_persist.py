"""ST-REGIME 第二段 RED→GREEN 夹具: 留痕表 + config 键化 + 产线幂等。"""
import json
from unittest.mock import MagicMock, patch

import pytest

from strategy_layer.regime.persist import (
    EMOTION_CYCLE_DDL,
    build_daily_series,
    load_cycle_params,
    run_emotion_cycle_daily,
)


class TestConfigKeys:
    def test_yaml_overrides_code_defaults(self):
        # config 键化 (③): yaml 权威, 代码常量回落
        cfg = {"strategy.regime.emotion_cycle": {"zt_climax_floor": 100}}
        p = load_cycle_params(cfg)
        assert p["zt_climax_floor"] == 100          # yaml 覆盖
        assert p["ladder_ice_floor"] == 200           # 其余回落代码缺省

    def test_no_config_returns_defaults(self):
        p = load_cycle_params(None)
        assert p["zt_climax_floor"] == 90
        assert p["slope_window"] == 3

    def test_type_coercion(self):
        # yaml 数值以字符串载入时按代码缺省类型强转
        cfg = {"strategy.regime.emotion_cycle": {"slope_window": "5"}}
        p = load_cycle_params(cfg)
        assert p["slope_window"] == 5

    def test_classify_accepts_params(self):
        from strategy_layer.regime.emotion_cycle import classify_emotion_cycle
        s = [{"date": "2026-09-01", "zt": 95, "dt": 2, "ladder_size": 250},
             {"date": "2026-09-02", "zt": 96, "dt": 1, "ladder_size": 251},
             {"date": "2026-09-03", "zt": 97, "dt": 1, "ladder_size": 252}]
        # 缺省参数: zt=97 撞 climax 线 90 → climax
        assert classify_emotion_cycle(s)["stage"] == "climax"
        # 抬线到 100: 同序列落趋势档 (recovery)
        assert classify_emotion_cycle(s, params={
            "zt_climax_floor": 100, "ladder_ice_floor": 200, "zt_ice_ceiling": 30,
            "dt_ebb_floor": 30, "slope_window": 3, "ferment_slope_min": 15,
        })["stage"] == "recovery"


class TestPersistTable:
    def test_ddl_shape(self):
        # 表形态锁: date PK + 五阶段留痕列 + 证据 JSON
        assert "emotion_cycle_daily" in EMOTION_CYCLE_DDL
        assert "date TEXT PRIMARY KEY" in EMOTION_CYCLE_DDL
        assert "evidence_json" in EMOTION_CYCLE_DDL
        assert "ladder_last" in EMOTION_CYCLE_DDL

    def test_run_persists_idempotent(self):
        db = MagicMock()
        day = "2026-09-11"
        series = [{"date": "2026-09-09", "zt": 49, "dt": 7, "ladder_size": 250},
                  {"date": "2026-09-10", "zt": 60, "dt": 5, "ladder_size": 250},
                  {"date": day, "zt": 95, "dt": 2, "ladder_size": 252}]
        with patch("strategy_layer.regime.persist.build_daily_series",
                   return_value=series):
            r = run_emotion_cycle_daily(db, date=day)
        assert r["stage"] == "climax"
        assert r["persisted"] is True
        # DDL 建表 + INSERT 两句都发出
        sqls = [c.args[0] for c in db.get_connection.return_value.execute.call_args_list
                if c.args]
        assert any("CREATE TABLE IF NOT EXISTS emotion_cycle_daily" in s for s in sqls)
        assert any("INSERT INTO emotion_cycle_daily" in s for s in sqls)
        assert any("ON CONFLICT(date) DO UPDATE" in s for s in sqls)  # 幂等


class _R:
    """模拟 execute 结果对象 (链 .fetchall())。"""
    def __init__(self, rows):
        self._rows = rows
    def fetchall(self):
        return self._rows


class TestBuildSeries:
    def test_series_asc_and_ladder_join(self):
        db = MagicMock()
        conn = db.get_connection.return_value
        conn.execute.side_effect = [
            _R([("2026-09-11", 40, 21), ("2026-09-10", 60, 5), ("2026-09-09", 49, 7)]),
            _R([("2026-09-09", 250), ("2026-09-11", 252)]),
        ]
        s = build_daily_series(db, "2026-09-11")
        assert [d["date"] for d in s] == ["2026-09-09", "2026-09-10", "2026-09-11"]
        assert s[-1]["ladder_size"] == 252
        assert s[0]["ladder_size"] == 250  # 09-09 有快照
        assert s[1]["ladder_size"] == 250  # 09-10 缺快照 → 缺省 250

    def test_empty_on_db_error(self):
        db = MagicMock()
        db.get_connection.side_effect = Exception("db down")
        assert build_daily_series(db, "2026-09-11") == []


class TestRunDegradation:
    def test_insufficient_series_unknown_honest(self):
        db = MagicMock()
        with patch("strategy_layer.regime.persist.build_daily_series",
                   return_value=[{"date": "2026-09-11", "zt": 40, "dt": 21,
                                  "ladder_size": 250}]):
            r = run_emotion_cycle_daily(db, date="2026-09-11")
        assert r["stage"] == "unknown"
        assert r["persisted"] is True  # unknown 也落库 (诚实留痕非静默跳过)
