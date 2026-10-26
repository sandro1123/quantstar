"""R0: 生命线读模型 lifeline.py 红绿测试。

 hermetic: 内存/临时 sqlite, 不碰生产库 (conftest QS_TEST_DB 体系之外自洽)。
 覆盖: ID 锚点降级 / 阶段映射 / 事件流合并排序与噪声剔除 / 归因 join /
       只读强制 / D7 连续性窗口 / 同股多计划 ID 唯一。
"""
import sqlite3

import pytest

from report_layer.lifeline import (
    Lifeline,
    build_lifelines,
    is_open,
    plan_anchor,
)

POOL_COLS = """
    code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
    entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
    entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
    dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
    exit_price REAL, exit_date TEXT, net_return_pct REAL, review_quality TEXT
"""


def _mkdb() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(f"""
        CREATE TABLE stock_pool ({POOL_COLS});
        CREATE TABLE action_records (
            id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
            price REAL, reason TEXT, created_at TEXT);
        CREATE TABLE intraday_checks (
            id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
            current_price REAL, trigger_price REAL, status TEXT,
            detail TEXT, check_time TEXT);
        CREATE TABLE recommendation_retrospect (
            prediction_id INTEGER, code TEXT, rec_date TEXT, verdict TEXT,
            hit_5d INTEGER, hit_10d INTEGER,
            fwd_return_5d REAL, fwd_return_10d REAL);
    """)
    return con


def _seed(con: sqlite3.Connection) -> None:
    # A: entry_plan_date 空 → 锚点降级 added_date；全链路事件 + 归因命中
    con.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600001", "测试甲", "active", "2026-08-26", "2026-08-26 08:00:00",
         None, 10.0, 10.5, "2026-08-28", 10.2, "2026-08-27", 9.5, None,
         None, None, 12.0, None, None, None, None))
    # B: entry_plan_date 有值 → 用它作锚点；已退出；无归因
    con.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600002", "测试乙", "exited", "2026-08-24", "2026-08-24 08:00:00",
         "2026-08-25", 20.0, 21.0, "2026-08-27", 20.5, "2026-08-26", 19.0,
         None, None, None, None, 19.8, "2026-08-27", -3.4, "poor"))
    # C: 观察期最小行
    con.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600003", "测试丙", "watch", "2026-08-28", "2026-08-28 08:00:00",
         None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None))
    # D: 同股不同计划日 → 两条生命线
    con.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600001", "测试甲", "watch", "2026-08-20", "2026-08-20 08:00:00",
         None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None))

    con.executemany(
        "INSERT INTO action_records (code, action_type, price, reason, created_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("600001", "add_to_watch", None, "入池", "2026-08-26 08:00:01"),
            ("600001", "set_entry_range", None, "设区间", "2026-08-26 08:30:00"),
            ("600001", "entry", 10.2, "触价买入", "2026-08-27 09:35:03"),
            ("600002", "exit", 19.8, "止损", "2026-08-27 14:50:00"),
        ])
    con.executemany(
        "INSERT INTO intraday_checks (code, check_type, current_price, "
        "trigger_price, status, detail, check_time) VALUES (?,?,?,?,?,?,?)",
        [
            ("600001", "entry", 10.2, 10.5, "triggered", "入区间",
             "2026-08-27 09:35:02"),
            # 噪声: no_action 不进事件流
            ("600001", "entry", 9.0, 10.5, "no_action", "未触价",
             "2026-08-26 10:00:00"),
            ("600002", "exit", 19.1, 19.0, "triggered", "破止损",
             "2026-08-27 14:49:00"),
        ])
    con.executemany(
        "INSERT INTO recommendation_retrospect VALUES (?,?,?,?,?,?,?,?)",
        [
            (77, "600001", "2026-08-26", "buy", 1, None, 0.032, None),
            # 同 (code, rec_date) 更晚的 prediction_id 覆盖 (确定性)
            (91, "600001", "2026-08-26", "buy", 1, None, 0.032, None),
            (50, "", "2026-07-21", "buy", None, None, None, None),
        ])


@pytest.fixture()
def seeded() -> sqlite3.Connection:
    con = _mkdb()
    _seed(con)
    return con


def _by_id(lifelines, lid):
    m = {ll.lifeline_id: ll for ll in lifelines}
    assert lid in m, f"{lid} not in {list(m)}"
    return m[lid]


def test_id_anchor_prefers_entry_plan_date(seeded):
    lls = build_lifelines(seeded)
    assert _by_id(lls, "600002@2026-08-25").code == "600002"


def test_id_anchor_falls_back_to_added_date(seeded):
    lls = build_lifelines(seeded)
    assert _by_id(lls, "600001@2026-08-26").code == "600001"


def test_same_code_multi_plan_distinct_ids(seeded):
    lls = build_lifelines(seeded)
    ids = {ll.lifeline_id for ll in lls if ll.code == "600001"}
    assert ids == {"600001@2026-08-26", "600001@2026-08-20"}


def test_stage_mapping_all_statuses(seeded):
    lls = build_lifelines(seeded)
    assert _by_id(lls, "600001@2026-08-26").stage == "持有"
    assert _by_id(lls, "600002@2026-08-25").stage == "已退出"
    assert _by_id(lls, "600003@2026-08-28").stage == "观察"
    seeded.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600004", "丁", "pending_entry", "2026-08-28", "2026-08-28 09:00:00",
         None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None))
    seeded.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("600005", "戊", "exit_pending", "2026-08-28", "2026-08-28 09:00:00",
         None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None))
    lls = build_lifelines(seeded)
    assert _by_id(lls, "600004@2026-08-28").stage == "待入场"
    assert _by_id(lls, "600005@2026-08-28").stage == "待退出"


def test_events_merged_sorted_noise_excluded(seeded):
    ll = _by_id(build_lifelines(seeded), "600001@2026-08-26")
    labels = [(e.kind, e.label) for e in ll.events]
    assert labels == [
        ("action", "add_to_watch"),
        ("action", "set_entry_range"),
        ("check", "触价:entry"),      # 09:35:02 先于 entry action 09:35:03
        ("action", "entry"),
    ]
    assert all("no_action" not in (e.detail or "") for e in ll.events)
    ts = [e.ts for e in ll.events]
    assert ts == sorted(ts)


def test_retrospect_attached_on_exact_date_latest_prediction(seeded):
    ll = _by_id(build_lifelines(seeded), "600001@2026-08-26")
    assert ll.retrospect is not None
    assert ll.retrospect.prediction_id == 91
    assert ll.retrospect.hit_5d == 1
    assert ll.retrospect.fwd_return_5d == pytest.approx(0.032)
    # B 计划日 2026-08-25 无归因 → None
    assert _by_id(build_lifelines(seeded), "600002@2026-08-25").retrospect is None


def test_is_open_valid_until_window(seeded):
    """D7: 连续性窗口 = 未退出 且 valid_until 未过期。"""
    lls = build_lifelines(seeded)
    active = _by_id(lls, "600001@2026-08-26")     # valid_until 2026-08-28
    exited = _by_id(lls, "600002@2026-08-25")
    watch_no_until = _by_id(lls, "600003@2026-08-28")
    assert is_open(active, as_of="2026-08-27") is True
    assert is_open(active, as_of="2026-08-29") is False   # 过 valid_until
    assert is_open(exited, as_of="2026-08-26") is False   # 已退出
    assert is_open(watch_no_until, as_of="2026-09-30") is True  # 无窗口=长期观察


def test_readonly_connection_enforced(tmp_path):
    """生产形态: file:...?mode=ro 只读连接可构建, 且该连接写必炸。"""
    dbf = tmp_path / "ro.db"
    con = sqlite3.connect(dbf)
    _seed(_mkdb_into(con))
    con.commit()
    con.close()
    ro = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True)
    lls = build_lifelines(ro)
    assert len(lls) == 4
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO stock_pool (code) VALUES ('x')")


def _mkdb_into(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(f"""
        CREATE TABLE stock_pool ({POOL_COLS});
        CREATE TABLE action_records (
            id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
            price REAL, reason TEXT, created_at TEXT);
        CREATE TABLE intraday_checks (
            id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
            current_price REAL, trigger_price REAL, status TEXT,
            detail TEXT, check_time TEXT);
        CREATE TABLE recommendation_retrospect (
            prediction_id INTEGER, code TEXT, rec_date TEXT, verdict TEXT,
            hit_5d INTEGER, hit_10d INTEGER,
            fwd_return_5d REAL, fwd_return_10d REAL);
    """)
    return con


def test_plan_anchor_pure_function(seeded):
    seeded.row_factory = sqlite3.Row
    row = seeded.execute(
        "SELECT * FROM stock_pool WHERE code='600002'").fetchone()
    assert plan_anchor(row) == "2026-08-25"
    row2 = seeded.execute(
        "SELECT * FROM stock_pool WHERE code='600003'").fetchone()
    assert plan_anchor(row2) == "2026-08-28"
