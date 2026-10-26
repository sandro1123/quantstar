"""BT-03: backtest_runs 落库 + 版本管理测试

覆盖: BacktestRunStore 写读、config 门控 (默认关不建表不写库)、
manifest 最小实现 (config hash 真算 / data_hash 无快照机制显式 None)。
用 in-memory sqlite, 不碰生产库。
"""
import json
import sqlite3
from types import SimpleNamespace

from backtest_layer import BacktestLayer
from backtest_layer.core.models import BacktestInput, BacktestReport
from backtest_layer.results.persistence import BacktestRunStore, config_hash


class _Db:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self._conn


class FakeDataLayer:
    """snapshot 返回固定 kline; config/db 按测试需要装配"""

    def __init__(self, config=None, db=None):
        self.config = config
        self.db = db

    def snapshot(self, date: str):
        return SimpleNamespace(klines=[SimpleNamespace(code="600519", close=10.0)])


class FakeConfig:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _report(**kw) -> BacktestReport:
    base = dict(mode="hold", date_range=("2026-08-03", "2026-08-07"),
                total_return=0.05, annual_return=0.12, sharpe=1.2,
                max_drawdown=-0.03, win_rate=0.6, total_trades=10,
                benchmark_return=0.02, excess_return=0.03)
    base.update(kw)
    return BacktestReport(**base)


class TestConfigHash:
    def test_deterministic(self):
        params = {"version": "v1", "weights": {"mom": 0.5}}
        assert config_hash(params) == config_hash(dict(params))

    def test_key_order_insensitive(self):
        a = config_hash({"a": 1, "b": 2})
        b = config_hash({"b": 2, "a": 1})
        assert a == b

    def test_empty_is_none(self):
        assert config_hash(None) is None
        assert config_hash({}) is None


class TestBacktestRunStore:
    def test_save_and_get(self):
        db = _Db()
        store = BacktestRunStore(db)
        bt_input = BacktestInput(mode="hold", codes=["600519"],
                                 date_range=("2026-08-03", "2026-08-07"),
                                 strategy_params={"version": "v1", "k": 1})
        run_id = store.save(_report(), bt_input=bt_input, duration_ms=123)
        row = store.get_run(run_id)
        assert row is not None
        assert row["mode"] == "hold"
        assert row["strategy_version"] == "v1"
        assert row["strategy_config_hash"] == config_hash({"version": "v1", "k": 1})
        assert row["date_range_start"] == "2026-08-03"
        assert row["universe_size"] == 1
        assert row["sharpe"] == 1.2
        assert row["duration_ms"] == 123
        # data_hash: 无数据快照机制 → 显式 None, 不编造指纹
        assert row["data_hash"] is None
        manifest = json.loads(row["manifest_json"])
        assert manifest["data_date_range"] == ["2026-08-03", "2026-08-07"]
        assert manifest["data_hash"] is None

    def test_save_without_input(self):
        """组合聚合等无单一 input 的场景: universe_size/版本为 None 不崩"""
        store = BacktestRunStore(_Db())
        run_id = store.save(_report(mode="portfolio"))
        row = store.get_run(run_id)
        assert row["universe_size"] is None
        assert row["strategy_version"] is None

    def test_list_and_latest(self):
        store = BacktestRunStore(_Db())
        store.save(_report(mode="hold"))
        store.save(_report(mode="signal"))
        store.save(_report(mode="hold"))
        assert len(store.list_runs()) == 3
        assert len(store.list_runs(mode="hold")) == 2
        assert store.latest_run()["mode"] == "hold"
        assert store.latest_run(mode="signal")["mode"] == "signal"

    def test_wf_fields(self):
        store = BacktestRunStore(_Db())
        run_id = store.save(_report(mode="walk_forward",
                                    wf_result={"efficiency": 0.7, "consistency": 0.8}))
        row = store.get_run(run_id)
        assert row["wf_efficiency"] == 0.7
        assert row["wf_consistency"] == 0.8


class TestLayerGate:
    def _input(self) -> BacktestInput:
        return BacktestInput(mode="hold", codes=["600519"],
                             date_range=("2026-08-03", "2026-08-07"))

    def test_default_off_no_table(self):
        """默认关闭: 不建表不写库, 行为与修复前一致"""
        db = _Db()
        layer = BacktestLayer(data_layer=FakeDataLayer(config=FakeConfig({}), db=db))
        report = layer.run(self._input())
        assert report.mode == "hold"
        tables = db.get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='backtest_runs'").fetchall()
        assert tables == []

    def test_no_config_no_write(self):
        layer = BacktestLayer(data_layer=FakeDataLayer(config=None, db=_Db()))
        report = layer.run(self._input())
        assert report.mode == "hold"

    def test_enabled_writes_row(self):
        """启用后: run 出口统一落库, run_id 回填"""
        db = _Db()
        cfg = FakeConfig({"backtest.persistence.enabled": True})
        layer = BacktestLayer(data_layer=FakeDataLayer(config=cfg, db=db))
        report = layer.run(self._input())
        assert report.run_id != ""
        row = db.get_connection().execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?",
            (report.run_id,)).fetchone()
        assert row is not None
        assert row["mode"] == "hold"
        assert row["universe_size"] == 1
        assert row["duration_ms"] is not None

    def test_enabled_no_db_degrades_gracefully(self):
        cfg = FakeConfig({"backtest.persistence.enabled": True})
        layer = BacktestLayer(data_layer=FakeDataLayer(config=cfg, db=None))
        report = layer.run(self._input())
        assert report.mode == "hold"  # 无 db 时跳过落库, 回测照常
