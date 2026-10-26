# -*- coding: utf-8 -*-
"""记忆层消费面修补 — 逐专家效果级验收 (注册表驱动, 未来新专家自动覆盖)。

病根: ALL_EXPERTS 六类仅 SentimentExpert 实现 set_memory_db,
入口 agent_layer.py hasattr 探测把其余五类静默跳过 → 「六专家注入」实为 1。
判例: hasattr 探测式接线的验收必须逐类断言效果, 禁止「机制对=全通」。

验收锚 (效果级, 不验机制):
  ① 注册表每个专家实例都有 set_memory_db (显式断言, 拒绝静默跳过)
  ② 播种 lesson 后, analyze 的 LLM 腿输入 data_summary 真含该 lesson
     (monkeypatch 截获 _llm_expert_opinion 入参; 返 None 落规则腿)
  ③ 注入流量计数 read_hits 真 +1 (memory_metrics 双向可观测面)
"""
import sqlite3
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from agent_layer.debate import experts as experts_mod  # noqa: E402
from agent_layer.debate.experts import ALL_EXPERTS  # noqa: E402
from memory_layer.store import MemoryStore  # noqa: E402
from memory_layer.wire import get_read_hits  # noqa: E402

_MARKER = "上次骗过我_MARKER_60911"
_CODE = "600519"


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


def _seed(db):
    MemoryStore(db).write(scope=f"stock:{_CODE}", kind="lesson",
                          content=_MARKER, evidence="test-evidence-60911",
                          confidence=0.9)


def _spy_factory(captured):
    def _spy(**kw):
        captured.update(kw)
        return None  # LLM 腿缺席 → 落规则腿, 不炸链
    return _spy


def test_all_experts_have_set_memory_db():
    """① 注册表逐类显式断言 — hasattr 静默跳过的对立面。"""
    for cls in ALL_EXPERTS:
        assert hasattr(cls(), "set_memory_db"), (
            f"{cls.__name__} 缺 set_memory_db — 入口 hasattr 探测将静默跳过, "
            f"记忆注入对它是死面")


def test_every_expert_consumes_seeded_lesson(db, monkeypatch):
    """② 效果级: 每个专家 analyze 的决策输入真含已播种 lesson。"""
    monkeypatch.setattr(experts_mod, "_llm_expert_opinion",
                        _spy_factory(captured := {}))
    for cls in ALL_EXPERTS:
        _seed(db)
        exp = cls()
        exp.set_memory_db(db)  # ① 通过后此调用即真接线
        captured.clear()
        exp.analyze({"code": _CODE, "date": "2026-09-10",
                     "turnover_ratio": 6.0, "pe": 15.0})
        summary = (captured.get("data") or {}).get("data_summary", "")
        assert _MARKER in summary, (
            f"{cls.__name__} analyze 未消费已播种 lesson "
            f"(LLM 腿 data_summary 无 marker) — 注入面死")


def test_injection_bumps_read_hits(db, monkeypatch):
    """③ 可观测面: 注入发生即 read_hits 计数 +1。"""
    monkeypatch.setattr(experts_mod, "_llm_expert_opinion",
                        _spy_factory({}))
    _seed(db)
    before = get_read_hits(db)
    exp = ALL_EXPERTS[0]()
    exp.set_memory_db(db)
    exp.analyze({"code": _CODE, "date": "2026-09-10"})
    assert get_read_hits(db) > before, "注入后 read_hits 未 +1 — 可观测面死"
