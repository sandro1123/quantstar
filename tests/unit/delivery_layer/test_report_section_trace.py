"""B席 ⑤: 晨报段留痕 (report_section_trace) — TDD 真红

需求 (统筹 sm-3b655da8ee22416d):
  (a) 每次推送把段清单 + 各段字符数落表
  (b) 零字符段必须显式标记 (不能只是"段在")
  (c) 留痕要能查「近 N 次逐段在位矩阵」

设计: delivery_log 姊妹表 report_section_trace (message_id, content_type, date,
      section_index, section_name, char_count, is_empty, degraded, created_at)
      写者 = DeliveryLogStore.record_sections() (与 record 同点, 失败不阻塞投递)
"""
import pytest

from common.database import Database
from delivery_layer.log_store import DeliveryLogStore
from delivery_layer.message import Message


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_db()
    return d


def _msg_with_sections(sections, content_type="morning_brief"):
    return Message(content={"sections": sections, "date": "2026-09-16"},
                   content_type=content_type, title="t")


def test_record_sections_persists_each_section(db):
    """(a) 段清单 + 各段字符数落表"""
    store = DeliveryLogStore(db)
    msg = _msg_with_sections([
        {"name": "ActionCandidatesSection", "title": "🎯 今日行动候选", "body": "abcde"},
        {"name": "ConclusionSection", "title": "结论", "body": "xy"},
    ])
    store.record_sections(msg, date="2026-09-16")
    rows = store.section_trace("2026-09-16")
    assert len(rows) == 2
    assert rows[0]["section_name"] == "ActionCandidatesSection"
    # char_count = title+subtitle+body: "🎯 今日行动候选"(8) + "abcde"(5) = 13
    assert rows[0]["char_count"] == 13
    assert rows[0]["is_empty"] == 0
    assert rows[1]["section_name"] == "ConclusionSection"
    # "结论"(2) + "xy"(2) = 4
    assert rows[1]["char_count"] == 4


def test_empty_section_explicitly_marked(db):
    """(b) 零字符段必须显式标记 is_empty=1 (假绿判据)"""
    store = DeliveryLogStore(db)
    msg = _msg_with_sections([
        {"name": "RiskComplianceSection", "title": "", "body": ""},
        {"name": "ConclusionSection", "title": "结论", "body": "有内容"},
    ])
    store.record_sections(msg, date="2026-09-16")
    rows = store.section_trace("2026-09-16")
    empty = [r for r in rows if r["section_name"] == "RiskComplianceSection"][0]
    assert empty["is_empty"] == 1, "空段必须显式标记, 不能只记'段在'"
    assert empty["char_count"] == 0


def test_degraded_section_marked(db):
    """降级段也须标记 (与 AUD-016 面衔接)"""
    store = DeliveryLogStore(db)
    msg = _msg_with_sections([
        {"name": "NewsSection", "title": "要闻", "body": "",
         "degraded": True, "degrade_reason": "取数失败"},
    ])
    store.record_sections(msg, date="2026-09-16")
    rows = store.section_trace("2026-09-16")
    assert rows[0]["degraded"] == 1


def test_section_matrix_recent_runs(db):
    """(c) 可查近 N 次逐段在位矩阵"""
    store = DeliveryLogStore(db)
    for d, names in [("2026-09-14", ["A", "B"]),
                     ("2026-09-15", ["A", "B", "C"]),
                     ("2026-09-16", ["A"])]:
        msg = _msg_with_sections([{"name": n, "body": "x"} for n in names])
        store.record_sections(msg, date=d)
    m = store.section_matrix(content_type="morning_brief", limit=5)
    assert set(m["sections"]) >= {"A", "B", "C"}
    assert m["runs"] == ["2026-09-14", "2026-09-15", "2026-09-16"]
    # C 段仅 09-15 在位
    assert m["matrix"]["C"] == [0, 1, 0]


def test_record_sections_never_raises_on_bad_content(db):
    """留痕失败绝不阻塞投递 (与 record 同纪律)"""
    store = DeliveryLogStore(db)
    msg = Message(content=None, content_type="x", title="t")
    store.record_sections(msg, date="2026-09-16")  # 不抛
    msg2 = Message(content={"sections": "not-a-list"}, content_type="x", title="t")
    store.record_sections(msg2, date="2026-09-16")  # 不抛


def test_delivery_log_record_still_works(db):
    """零回归: 既有 record 面不动"""
    from delivery_layer.channels.base import DeliveryResult
    store = DeliveryLogStore(db)
    store.record(DeliveryResult(message_id="m1", channel="feishu_group",
                                status="success"), "morning_brief")
    assert len(store.recent(5)) == 1


def test_v7_migration_creates_table():
    """B席 09-16: v7 迁移路径须建出 report_section_trace。

    变异法发现: 上面 6 测全走 Database() 直建表 (不经 ensure_schema),
    真删 v7 时 6 测全绿 = 测试盲区。本测补上迁移路径覆盖。
    """
    from common.database import Database as _DB
    from execution_layer.migrations import ensure_schema
    db = _DB(":memory:")
    db.init_db()
    ensure_schema(db)
    conn = db.get_connection()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='report_section_trace'").fetchone(), "v7 迁移须建 report_section_trace"
    # 且可写可读 (端到端)
    store = DeliveryLogStore(db)
    msg = _msg_with_sections([{"name": "ConclusionSection", "body": "x"}])
    store.record_sections(msg, date="2026-09-16")
    assert len(store.section_trace("2026-09-16")) == 1
