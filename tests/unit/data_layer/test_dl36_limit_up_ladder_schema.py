"""DL-36: limit_up_ladder schema — snapshot_date (不是 date) 才是正确日期列

判定: **查询写错, 不是表建错**。limit_up_ladder 是 30 日连板矩阵快照:
一个 snapshot_date 下横跨约 30 个 trade_date, 所以必须用复合主键
(snapshot_date, trade_date, board_level, code) —— 单一 `date` 列在此有歧义。

`no such column: date` 的报错来自"假设本表跟其余 6 张冷采集表一样用 date"
的通用查询; 生产代码所有路径早已用 snapshot_date (本测试对真实库端到端验证过:
check_collect_freshness 393 行 fresh / get_limit_ladder 30 天 / table_catalog ok)。

零消费方 (冷采集数据) —— 适用铁律 #13 例外: 涨停天梯是不可回补的日频数据,
即使暂无消费方也该采。本测试钉住 schema 事实, 防止有人把映射改回 date。
"""
import sqlite3

from data_layer.health.readiness import DataReadiness


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _seed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE limit_up_ladder (
            snapshot_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            board_level TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            board_num INTEGER,
            seal_nextday INTEGER,
            sign_level INTEGER,
            source TEXT DEFAULT 'fuyao',
            PRIMARY KEY (snapshot_date, trade_date, board_level, code)
        );
        CREATE TABLE limit_up_ladder_meta (
            snapshot_date TEXT PRIMARY KEY,
            window_length INTEGER,
            board_caps_json TEXT,
            date_list_json TEXT,
            source TEXT DEFAULT 'fuyao'
        );
    """)
    # 一个快照横跨 3 个 trade_date (连板矩阵的本质)
    for td in ("2026-08-19", "2026-08-20", "2026-08-21"):
        conn.execute(
            "INSERT INTO limit_up_ladder "
            "(snapshot_date, trade_date, board_level, code, name, board_num) "
            "VALUES ('2026-08-21', ?, '2', '300001', 'x', 2)", (td,))
    conn.execute(
        "INSERT INTO limit_up_ladder_meta (snapshot_date, window_length) "
        "VALUES ('2026-08-21', 30)")
    conn.commit()
    return _FakeDB(conn)


def test_collect_tables_maps_ladder_to_snapshot_date():
    """日期列映射必须是 snapshot_date —— 改回 date 会让新鲜度检查全表报错。"""
    assert DataReadiness.COLLECT_TABLES["limit_up_ladder"] == "snapshot_date"


def test_ladder_has_no_date_column():
    """钉住 schema 事实: 本表没有 date 列 (这正是通用查询报错的根因)。"""
    db = _seed()
    conn = db.get_connection()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(limit_up_ladder)")}
    assert "date" not in cols
    assert {"snapshot_date", "trade_date"} <= cols


def test_freshness_check_uses_snapshot_date():
    """check_collect_freshness 走 snapshot_date, 能查到当日快照。"""
    db = _seed()
    fresh = DataReadiness(db).check_collect_freshness("2026-08-21")
    assert fresh["limit_up_ladder"]["fresh"] is True
    assert fresh["limit_up_ladder"]["rows"] == 3


def test_read_api_get_limit_ladder_groups_by_trade_date():
    """读 API 按 snapshot_date 取数、按 trade_date 分组 (30 日矩阵展开)。"""
    from data_layer._loader import DataLoader

    db = _seed()
    loader = DataLoader(db)
    result = loader.get_limit_ladder("2026-08-21")
    assert result["meta"] is not None
    assert len(result["days"]) == 3
    dates = {d["date"] for d in result["days"]}
    assert dates == {"2026-08-19", "2026-08-20", "2026-08-21"}
