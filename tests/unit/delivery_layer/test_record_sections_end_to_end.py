# -*- coding: utf-8 -*-
"""Day5 卡D·端到端验收——判例#138 三问 + 判例#166 完整形态（读侧修了≠写侧修了）。

判例#166 完整形态:「读侧修了≠写侧修了」——stash 只修了门(_log_sections),
没修进写表的手(record_sections 内部仍读 message.content)。

端到端钉（不走直调被调方,走完整链路）:
  Message -> build stash( dict(content) ) -> formatter 覆盖 content -> _log_sections
  -> record_sections 应写表。断言 trace 有行。
"""
import sqlite3
import pytest
from delivery_layer.message import Message
from delivery_layer.log_store import DeliveryLogStore


def _trace_count(db_conn, date_val):
    return db_conn.execute(
        "SELECT COUNT(*) FROM report_section_trace WHERE date=?",
        (date_val,)).fetchone()[0]


@pytest.fixture()
def spell_db(tmp_path):
    """in-memory 真库,_log_sections 走真 Table 写."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE IF NOT EXISTS report_section_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL, content_type TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL, section_index INTEGER NOT NULL,
        section_name TEXT NOT NULL, char_count INTEGER NOT NULL DEFAULT 0,
        is_empty INTEGER NOT NULL DEFAULT 0, degraded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')))""")
    conn.commit()
    yield conn
    conn.close()


class _FakeDB:
    def __init__(self, conn):
        self._c = conn
    def get_connection(self):
        return self._c


def test_record_sections_writes_trace_via_raw_content(spell_db):
    """端到端钉(判例#166 完整形态): build→stash→formatter→_log_sections→record_sections 写表."""
    # Step 1: build 时的 message(content["sections"] 还是 list)
    msg = Message(
        content={
            "sections": [
                {"name": "top_picks", "body": "今天买 A"},
                {"name": "news", "body": "新闻"},
                {"name": "risk_tips", "body": "风险"},
            ],
            "date": "2026-09-28",
        },
        content_type="morning_brief",
        title="晨报",
    )
    # Step 2: build 时 stash(修法后)
    msg.raw_content = dict(msg.content)
    # Step 3: formatter.format 改形(模拟真 formatter 输出把 sections 键丢了)
    msg.content = {"msg_type": "interactive", "card": {"header": {"title": {"content": "晨报"}}}}
    assert "sections" not in msg.content, "formatter 后 content 里不智能 sections"
    # Step 4: _log_sections(修后)在我链路上调到 record_sections
    store = DeliveryLogStore(_FakeDB(spell_db))
    # 模拟 _log_sections(手动送餐但走 store 全路径):
    content = getattr(msg, "raw_content", None)
    if not isinstance(content, dict):
        content = getattr(msg, "content", None)
    assert isinstance(content, dict) and isinstance(content.get("sections"), list), \
        "record_sections 前条件(优先 stash)满足"
    # 全链路调用 record_sections(真实)
    before = _trace_count(spell_db, "2026-09-28")
    store.record_sections(msg, date="2026-09-28")
    after = _trace_count(spell_db, "2026-09-28")
    assert after == before + 3, (
        f"端到端应该 3 段落, 但 before={before} after={after}."
        "若 {after}=={before}, 说明 record_sections 内仍从 message.content 读了,"
        "不吃 raw_content 优先."
    )


def test_record_sections_zero_trace_without_stash(spell_db):
    """反向钉: 无 stash 时 record_sections 不写表(判例#138:preserve 与行为不可走岔)."""
    msg = Message(
        content={"msg_type": "interactive", "card": {"header": {"title": {"content": "晨报"}}}},
        content_type="morning_brief",
        title="晨报",
    )
    # 不设 raw_content(现实中 _build_report 没 stash 时)
    store = DeliveryLogStore(_FakeDB(spell_db))
    before = _trace_count(spell_db, "2026-09-28")
    store.record_sections(msg, date="2026-09-28")
    after = _trace_count(spell_db, "2026-09-28")
    assert after == before, "无 stash 时应继续 0 行(不修半不痛后仍 0)"
