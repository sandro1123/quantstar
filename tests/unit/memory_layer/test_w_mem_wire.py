# -*- coding: utf-8 -*-
"""记忆层案A通电微卡 — RED 先行验收测试 (:memory:, 无网络, 无 LLM 真调)。

四件 (派单 sm-6ddacf7b):
  ① 消费面: MemoryStore.read() 接入六专家决策前 — memories 注入 data_summary
  ② 喂料面: ic_feedback 结算后 factor 级 lesson 写入 (evidence 必填闸门)
  ③ 双向流量可观测: memory_read_hits / memory_write_hits 计数, 晨验可查
  ④ 案C PIT 留口: 写入带 ingested_at 字段位 (表列 + write 参数, 不施工 PIT 本体)
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from memory_layer.store import MemoryStore  # noqa: E402
from memory_layer import wire  # noqa: E402  ← RED: 模块尚不存在


# ---------------------------------------------------------------------------
# 夹具: :memory: 库 + memory_store (含 ingested_at 新列位)
# ---------------------------------------------------------------------------

def _mem_schema(conn):
    conn.execute("""
        CREATE TABLE memory_store (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT, kind TEXT, content TEXT, evidence TEXT,
            confidence REAL, created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT, status TEXT DEFAULT 'active',
            superseded_by INTEGER,
            ingested_at TEXT
        )""")
    conn.commit()


class _DB:
    """Database 同形轻夹具: get_connection() 返 sqlite3 原生连接。"""
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        _mem_schema(self._conn)

    def get_connection(self):
        return self._conn


@pytest.fixture
def db():
    return _DB()


# ---------------------------------------------------------------------------
# ④ PIT 留口 — ingested_at 字段位
# ---------------------------------------------------------------------------

def test_write_has_ingested_at_param(db):
    """write() 接受 ingested_at 关键字并落列; 缺省=now (BJT)。"""
    ms = MemoryStore(db)
    mid = ms.write("stock:920189", "lesson", "这股上次骗过我: 假突破",
                  evidence="backtest:2026-09-01:stop_loss=-3.2%",
                  confidence=0.8, ingested_at="2026-09-01 15:30:00")
    assert mid is not None
    row = db.get_connection().execute(
        "SELECT ingested_at FROM memory_store WHERE id=?", (mid,)).fetchone()
    assert row[0] == "2026-09-01 15:30:00"


def test_write_ingested_at_default_now(db):
    """缺省 ingested_at 落当前时刻 (非 NULL) — PIT 留口后字段恒有值。"""
    ms = MemoryStore(db)
    mid = ms.write("factor:roe_quality", "lesson", "factor 近20日 IC 转负",
                  evidence="ic_feedback:2026-09-08:mean_ic=-0.12")
    assert mid is not None
    row = db.get_connection().execute(
        "SELECT ingested_at FROM memory_store WHERE id=?", (mid,)).fetchone()
    assert row[0] is not None and row[0] != ""


# ---------------------------------------------------------------------------
# ① 消费面 — wire.inject_memories 进六专家 data_summary
# ---------------------------------------------------------------------------

def test_inject_memories_appends_lesson(db):
    """有记忆 → data_summary 尾部拼「历史教训」段, 六专家 prompt 可见。"""
    ms = MemoryStore(db)
    ms.write("stock:000001", "lesson", "上次假突破止损 -3%",
             evidence="backtest:2026-08:hit")
    data = {"code": "000001", "date": "2026-09-09",
            "data_summary": "PE 12.3, ROE 8%, 涨幅 +2.1%"}
    out = wire.inject_memories(db, data)
    assert "历史教训" in out["data_summary"]
    assert "假突破" in out["data_summary"]
    assert "PE 12.3" in out["data_summary"]  # 原摘要保留不被替换


def test_inject_memories_no_memories_untouched(db):
    """无记忆 → data_summary 原样返回 (零注入零扰动)。"""
    data = {"code": "999999", "date": "2026-09-09",
            "data_summary": "PE 12.3"}
    out = wire.inject_memories(db, data)
    assert out["data_summary"] == "PE 12.3"
    assert out is not data or "memories" not in out  # 不加空键


def test_inject_memories_factor_scope_also(db):
    """factor:<name> 域教训同注入 (派单口径: stock + factor 双域)。"""
    ms = MemoryStore(db)
    ms.write("factor:roe_quality", "lesson", "IC 连续转负, 降权处置过",
             evidence="ic_feedback:2026-08:mean_ic=-0.05")
    data = {"code": "000001", "date": "2026-09-09",
            "data_summary": "roe_quality=0.8", "factors": ["roe_quality"]}
    out = wire.inject_memories(db, data)
    assert "IC 连续转负" in out["data_summary"]


# ---------------------------------------------------------------------------
# ③ 双向流量可观测 — memory_read_hits 计数
# ---------------------------------------------------------------------------

def test_read_hits_counter(db):
    """inject_memories 读命中 → memory_read_hits +1 (写计数在 write 内)。"""
    ms = MemoryStore(db)
    ms.write("stock:000001", "lesson", "止损教训", evidence="bt:1")
    before = wire.get_read_hits(db)
    data = {"code": "000001", "date": "d", "data_summary": "x"}
    wire.inject_memories(db, data)
    after = wire.get_read_hits(db)
    assert after >= before + 1


def test_get_metrics_dual(db):
    """get_metrics 返 dict 含 read_hits/write_hits 两键 (晨验可查形态)。"""
    m = wire.get_metrics(db)
    assert isinstance(m, dict)
    assert "read_hits" in m and "write_hits" in m


# ---------------------------------------------------------------------------
# ② 喂料面 — ic_feedback 结算后 factor lesson 写入
# ---------------------------------------------------------------------------

def test_record_ic_lessons_writes_negative_ic(db):
    """IC 转负因子 → factor:<name> lesson 写入, evidence 带 calc_date+mean_ic。"""
    n = wire.record_ic_lessons(db, {"roe_quality": -0.12, "pe_ratio": 0.03},
                               sample_size=480, calc_date="2026-09-04")
    assert n == 1  # 只写转负者, 正 IC 不造教训
    row = db.get_connection().execute(
        "SELECT content, evidence, ingested_at FROM memory_store "
        "WHERE scope='factor:roe_quality' AND kind='lesson'").fetchone()
    assert row is not None
    assert "IC 转负" in row[0] or "转负" in row[0]
    assert "2026-09-04" in row[1] and "-0.12" in row[1]
    assert row[2] is not None  # ④ ingested_at 恒有值


def test_record_ic_lessons_no_negative_no_write(db):
    """全正 IC → 零写入 (无病不造记忆)。"""
    n = wire.record_ic_lessons(db, {"pe_ratio": 0.05}, sample_size=100,
                               calc_date="2026-09-04")
    assert n == 0


def test_record_ic_lessons_evidence_gate(db):
    """evidence 必填闸门在位: 空证据写手拒绝 (三铁律)。"""
    n = wire.record_ic_lessons(db, {"roe_quality": -0.12}, sample_size=10,
                               calc_date="", )
    # calc_date 空 → evidence 仍非空 (因子名+ic 值构成), 闸门由 MemoryStore 兜底
    # 断言: 要么拒写 (n==0), 要么行 evidence 非空
    if n:
        row = db.get_connection().execute(
            "SELECT evidence FROM memory_store WHERE kind='lesson'").fetchone()
        assert row[0].strip() != ""
