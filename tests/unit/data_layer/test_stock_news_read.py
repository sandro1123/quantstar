"""DL-20: 个股新闻读 API — :memory: 库, 无生产写

验证: 个股新闻返回/个股优先于市场级/无个股时市场级补足/日期过滤/limit/表缺失返回空
"""

import sqlite3

_SCHEMA = """
CREATE TABLE stock_news (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0,
    summary TEXT,
    PRIMARY KEY (code, date, title)
)
"""


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def get_connection(self):
        return self.conn


def _dl(db):
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    dl.db = db
    return dl


def _insert(db, code, date, title, sentiment="neutral", score=50.0):
    db.conn.execute(
        "INSERT INTO stock_news (code, date, title, source, sentiment, "
        "sentiment_score, summary) VALUES (?, ?, ?, 'test', ?, ?, 's')",
        (code, date, title, sentiment, score))
    db.conn.commit()


def test_stock_news_returned_with_fields():
    db = _FakeDB()
    _insert(db, "600519", "2026-08-18", "茅台利好", "positive", 80.0)
    rows = _dl(db).get_stock_news("600519", "2026-08-18")
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600519"
    assert r["title"] == "茅台利好"
    assert r["sentiment"] == "positive"
    assert r["sentiment_score"] == 80.0
    assert r["summary"] == "s"


def test_stock_news_before_market_news():
    """个股新闻排市场级 (code='') 之前"""
    db = _FakeDB()
    _insert(db, "", "2026-08-18", "市场新闻A")
    _insert(db, "600519", "2026-08-18", "个股新闻B")
    rows = _dl(db).get_stock_news("600519", "2026-08-18")
    assert [r["title"] for r in rows] == ["个股新闻B", "市场新闻A"]


def test_market_news_fills_when_no_stock_news():
    """无个股新闻时返回当日市场级新闻 (当前表内数据实况)"""
    db = _FakeDB()
    _insert(db, "", "2026-08-18", "市场新闻A")
    rows = _dl(db).get_stock_news("000001", "2026-08-18")
    assert len(rows) == 1
    assert rows[0]["title"] == "市场新闻A"


def test_other_stock_news_excluded():
    """他股新闻不混入"""
    db = _FakeDB()
    _insert(db, "000002", "2026-08-18", "万科新闻")
    _insert(db, "600519", "2026-08-18", "茅台新闻")
    rows = _dl(db).get_stock_news("600519", "2026-08-18")
    assert [r["title"] for r in rows] == ["茅台新闻"]


def test_date_filter():
    db = _FakeDB()
    _insert(db, "600519", "2026-08-17", "昨日新闻")
    _insert(db, "600519", "2026-08-18", "今日新闻")
    rows = _dl(db).get_stock_news("600519", "2026-08-18")
    assert [r["title"] for r in rows] == ["今日新闻"]


def test_limit_respected():
    db = _FakeDB()
    for i in range(30):
        _insert(db, "", "2026-08-18", f"市场新闻{i:02d}")
    rows = _dl(db).get_stock_news("600519", "2026-08-18", limit=10)
    assert len(rows) == 10


def test_missing_table_returns_empty():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)

    class _EmptyDB:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

        def get_connection(self):
            return self.conn

    dl.db = _EmptyDB()
    assert dl.get_stock_news("600519", "2026-08-18") == []
