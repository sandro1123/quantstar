"""DL-29 测试 — 数据质量把关: 异常值标记 + 轻量 DLQ

验证: 规则集(配置化)检出已知坏数据模式; 标记落 data_quality_flags 独立表;
DLQ 落 logs/dlq/<date>.jsonl 含原始记录/来源/原因/时间戳; 原值不被修改;
写入路径 (KlineRepo.batch_upsert / atomic_upsert_date) 接线; 重放脚本可重放。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer.quality import (
    DEFAULTS,
    DLQ,
    FlagStore,
    KlineQualityGate,
    check_kline_batch,
    load_quality_config,
)
from data_layer.repos.kline import KlineRepo

ROOT = Path(__file__).resolve().parents[3]


def _rec(code="600001", date="2026-08-20", **kw):
    base = dict(code=code, date=date, open=10.0, high=11.0, low=9.5,
                close=10.5, volume=100000, amount=1e6,
                pre_close=10.0, change_pct=5.0, turnover_rate=1.0)
    base.update(kw)
    return base


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


@pytest.fixture(autouse=True)
def _dlq_dir(tmp_path, monkeypatch):
    d = tmp_path / "dlq"
    monkeypatch.setenv("QS_DLQ_DIR", str(d))
    return d


class FakeConfig:
    def __init__(self, quality=None):
        self._q = quality or {}

    def get(self, key):
        return self._q if key == "data.quality" else None


# ── 规则集 ──

class TestRules:
    def test_pre_close_nonpositive_flagged(self):
        flags = check_kline_batch([_rec(pre_close=0)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "pre_close_nonpositive" in rules

    def test_change_pct_divergence_flagged(self):
        # close/pre_close-1 = 5%, 但 change_pct=0 (07-29 批次模式)
        flags = check_kline_batch([_rec(change_pct=0.0)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "change_pct_divergence" in rules

    def test_change_pct_within_threshold_clean(self):
        flags = check_kline_batch([_rec(change_pct=5.3)])  # 背离 0.3 < 1.0
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "change_pct_divergence" not in rules

    def test_close_out_of_range_flagged(self):
        flags = check_kline_batch([_rec(close=12.0)])  # > high=11
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "close_out_of_range" in rules

    def test_volume_negative_flagged(self):
        flags = check_kline_batch([_rec(volume=-1)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "volume_negative" in rules

    def test_high_below_low_flagged(self):
        flags = check_kline_batch([_rec(high=9.0, low=9.5, close=9.2)])
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "high_below_low" in rules

    def test_mad_outlier_flagged(self):
        # 19 条正常 (change_pct 与 close/pre_close 一致) + 1 条极端
        recs = [_rec(code=f"6{i:05d}", change_pct=5.0 + i * 0.01) for i in range(19)]
        recs.append(_rec(code="699999", change_pct=50.0))
        flags = check_kline_batch(recs)
        assert "699999" in flags
        assert any(f["rule"] == "mad_outlier" for f in flags["699999"])

    def test_clean_batch_no_flags(self):
        # change_pct=5.0x 与 close/pre_close-1=5% 一致, 批内 MAD 范围内
        recs = [_rec(code=f"6{i:05d}", change_pct=5.0 + i * 0.01) for i in range(20)]
        assert check_kline_batch(recs) == {}

    def test_batch_zeroed_change_pct_flagged(self):
        # 07-29 模式: 整批 change_pct=0 (含平盘股局部自洽的行)
        recs = [_rec(code=f"6{i:05d}", change_pct=0.0) for i in range(20)]
        flags = check_kline_batch(recs)
        assert len(flags) == 20
        assert all(any(f["rule"] == "batch_change_pct_zeroed" for f in fl)
                   for fl in flags.values())

    def test_normal_zero_ratio_not_flagged(self):
        # 正常日 ~3% 平盘: 不触发整批清零规则
        recs = [_rec(code=f"6{i:05d}", change_pct=5.0 + i * 0.01) for i in range(19)]
        recs.append(_rec(code="699998", change_pct=0.0, close=10.0))  # 1/20 平盘
        flags = check_kline_batch(recs)
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "batch_change_pct_zeroed" not in rules

    def test_original_records_not_modified(self):
        bad = _rec(pre_close=0, change_pct=0.0)
        snapshot = dict(bad)
        check_kline_batch([bad])
        assert bad == snapshot


# ── 配置化 ──

class TestConfig:
    def test_defaults_when_no_config(self):
        cfg = load_quality_config(FakeConfig())
        assert cfg["change_pct_divergence"] == DEFAULTS["change_pct_divergence"]

    def test_override_from_config(self):
        cfg = load_quality_config(FakeConfig({"change_pct_divergence": 2.5}))
        assert cfg["change_pct_divergence"] == 2.5

    def test_threshold_respected(self):
        cfg = load_quality_config(FakeConfig({"change_pct_divergence": 10.0}))
        flags = check_kline_batch([_rec(change_pct=0.0)], cfg)  # 背离 5 < 10
        rules = [f["rule"] for fl in flags.values() for f in fl]
        assert "change_pct_divergence" not in rules

    def test_disabled_gate_passthrough(self, db, _dlq_dir):
        gate = KlineQualityGate(db, config=FakeConfig({"enabled": False}))
        recs = [_rec(pre_close=0)]
        assert gate.process(recs) is recs
        assert not list(_dlq_dir.glob("*.jsonl"))


# ── flag 表 + DLQ ──

class TestFlagStoreAndDLQ:
    def test_flags_written_to_independent_table(self, db):
        gate = KlineQualityGate(db, config=FakeConfig())
        gate.process([_rec(pre_close=0)])
        rows = db.get_connection().execute(
            "SELECT code, field, flag, rule FROM data_quality_flags").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "pre_close"
        assert rows[0][3] == "pre_close_nonpositive"

    def test_source_table_values_untouched(self, db):
        repo = KlineRepo(db)
        gate = KlineQualityGate(db, config=FakeConfig())
        bad = _rec(pre_close=0)
        gate.process([bad])
        repo.batch_upsert([bad])
        row = db.get_connection().execute(
            "SELECT pre_close FROM daily_klines WHERE code='600001'").fetchone()
        assert row[0] == 0  # 原值保留, 只标记不清洗

    def test_dlq_jsonl_contains_required_fields(self, db, _dlq_dir):
        gate = KlineQualityGate(db, config=FakeConfig())
        gate.process([_rec(pre_close=0)], source="test_source")
        files = list(_dlq_dir.glob("2026-08-20.jsonl"))
        assert files, "DLQ 文件未按 <date>.jsonl 落盘"
        ev = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        for k in ("ts", "date", "source", "record", "flags"):
            assert k in ev, f"DLQ 缺字段 {k}"
        assert ev["source"] == "test_source"
        assert ev["record"]["pre_close"] == 0  # 原始记录完整
        assert ev["flags"][0]["rule"] == "pre_close_nonpositive"

    def test_flag_store_idempotent(self, db):
        store = FlagStore(db)
        flag = dict(code="600001", date="2026-08-20", field="pre_close",
                    flag="suspicious", reason="x", rule="pre_close_nonpositive")
        store.write_flags([flag])
        store.write_flags([flag])  # 重复写不炸不重复
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM data_quality_flags").fetchone()[0]
        assert n == 1

    def test_get_flags_filter(self, db):
        store = FlagStore(db)
        store.write_flags([
            dict(code="600001", date="2026-08-20", field="pre_close",
                 flag="suspicious", reason="x", rule="r1"),
            dict(code="000002", date="2026-08-21", field="close",
                 flag="outlier", reason="y", rule="r2"),
        ])
        assert len(store.get_flags(date="2026-08-20")) == 1
        assert len(store.get_flags(code="000002")) == 1


# ── 写入路径接线 ──

class TestWritePathWiring:
    def test_batch_upsert_flags_bad_records(self, db, _dlq_dir):
        repo = KlineRepo(db)
        repo.batch_upsert([_rec(pre_close=0)])
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM data_quality_flags").fetchone()[0]
        assert n >= 1, "batch_upsert 未接质量闸"
        # 原值仍入库
        row = db.get_connection().execute(
            "SELECT pre_close FROM daily_klines").fetchone()
        assert row[0] == 0

    def test_atomic_upsert_date_flags_bad_records(self, db, _dlq_dir):
        repo = KlineRepo(db)
        n = repo.atomic_upsert_date("2026-08-20", [_rec(pre_close=0)], min_rows=0)
        assert n == 1
        flags = db.get_connection().execute(
            "SELECT COUNT(*) FROM data_quality_flags").fetchone()[0]
        assert flags >= 1, "atomic_upsert_date 未接质量闸"

    def test_clean_write_no_flags_no_dlq(self, db, _dlq_dir):
        repo = KlineRepo(db)
        recs = [_rec(code=f"6{i:05d}", change_pct=5.0 + i * 0.01) for i in range(20)]
        repo.batch_upsert(recs)
        assert FlagStore(db).get_flags() == []
        assert not list(_dlq_dir.glob("*.jsonl"))


# ── 重放脚本 ──

class TestReplay:
    def test_replay_script_reinserts_records(self, db, _dlq_dir, tmp_path):
        gate = KlineQualityGate(db, config=FakeConfig())
        gate.process([_rec(pre_close=0)], source="replay_test")
        dlq_file = _dlq_dir / "2026-08-20.jsonl"
        assert dlq_file.exists()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "replay_dlq.py"),
             "--db", str(tmp_path / "t.db"), "--file", str(dlq_file), "--dry-run"],
            capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "replay" in r.stdout.lower()
        # dry-run 不写库 (表尚未建, 查询应无表或 0 行)
        try:
            n = db.get_connection().execute(
                "SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        assert n == 0
        # 实放
        r2 = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "replay_dlq.py"),
             "--db", str(tmp_path / "t.db"), "--file", str(dlq_file)],
            capture_output=True, text=True, env=env, timeout=60)
        assert r2.returncode == 0, r2.stderr
        n2 = db.get_connection().execute(
            "SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        assert n2 == 1, "重放未把原始记录写回"
