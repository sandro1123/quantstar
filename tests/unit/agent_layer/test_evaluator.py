"""Evaluator 单元测试 — 盘后评估器"""
from common.database import Database
from agent_layer.quality.evaluator import Evaluator


# ── 初始化 ──────────────────────────────────────────────


def test_init_no_db():
    ev = Evaluator()
    assert ev.repo.db is None


def test_init_with_db():
    db = Database(":memory:")
    ev = Evaluator(database=db)
    assert ev.repo.db is db


# ── 无数据库降级 ────────────────────────────────────────


def test_evaluate_no_db_returns_zeros():
    ev = Evaluator(database=None)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result == {"evaluated": 0, "hits": 0, "misses": 0, "skipped": 0}


# ── 空输入 ──────────────────────────────────────────────


def test_evaluate_no_predictions_for_date():
    db = Database(":memory:")
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result == {"evaluated": 0, "hits": 0, "misses": 0, "skipped": 0}


def test_evaluate_empty_forward_returns():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '推荐', 0.8)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {})
    assert result["evaluated"] == 0
    assert result["skipped"] == 1
    row = conn.execute("SELECT hit FROM agent_predictions").fetchone()
    assert row["hit"] is None


# ── 推荐/不推荐 verdict ─────────────────────────────────


def test_recommend_hit_positive_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '推荐', 0.8)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result["hits"] == 1
    assert result["misses"] == 0


def test_recommend_miss_negative_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '推荐', 0.8)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": -0.03})
    assert result["hits"] == 0
    assert result["misses"] == 1


def test_reject_hit_negative_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '不推荐', 0.7)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": -0.03})
    assert result["hits"] == 1
    assert result["misses"] == 0


def test_reject_miss_positive_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '不推荐', 0.7)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result["hits"] == 0
    assert result["misses"] == 1


# ── 英文 verdict 同义 ───────────────────────────────────


def test_approve_synonym():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', 'approve', 0.8)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.01})
    assert result["hits"] == 1


def test_reject_synonym():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', 'reject', 0.7)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": -0.01})
    assert result["hits"] == 1


# ── 中性 verdict ────────────────────────────────────────


def test_neutral_hit_small_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.01})
    assert result["hits"] == 1
    assert result["misses"] == 0


def test_neutral_hit_zero_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.0})
    assert result["hits"] == 1


def test_neutral_hit_negative_small_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": -0.01})
    assert result["hits"] == 1


def test_neutral_miss_large_positive_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result["hits"] == 0
    assert result["misses"] == 1


def test_neutral_miss_large_negative_return():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": -0.05})
    assert result["hits"] == 0
    assert result["misses"] == 1


def test_neutral_boundary_2pct():
    """|actual| == 0.02 应该是 miss (abs(actual) < 0.02 不包含边界)"""
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', '观望', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.02})
    assert result["hits"] == 0
    assert result["misses"] == 1


def test_neutral_english_synonym():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('000001', '2026-07-17', 'neutral', 0.5)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.001})
    assert result["hits"] == 1


# ── forward_returns 缺失的 code ─────────────────────────


def test_missing_code_skipped_not_zero():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES ('999999', '2026-07-17', '推荐', 0.8)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {})
    assert result["evaluated"] == 0
    assert result["skipped"] == 1


# ── 混合 verdict ────────────────────────────────────────


def test_mixed_verdicts():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000002', '2026-07-17', '不推荐', 0.7)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000003', '2026-07-17', '观望', 0.5)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000004', '2026-07-17', '推荐', 0.9)")
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {
        "000001": 0.05,   # 推荐+正 → hit
        "000002": -0.03,  # 不推荐+负 → hit
        "000003": 0.01,   # 观望+小 → hit
        "000004": -0.02,  # 推荐+负 → miss
    })
    assert result["evaluated"] == 4
    assert result["hits"] == 3
    assert result["misses"] == 1


# ── 只评估指定日期 ──────────────────────────────────────


def test_only_evaluates_target_date():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000002', '2026-07-18', '推荐', 0.8)")
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.05})
    assert result["evaluated"] == 1


# ── 已评估的不再重复 ────────────────────────────────────


def test_skips_already_evaluated():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence, hit) "
        "VALUES ('000001', '2026-07-17', '推荐', 0.8, 1)"
    )
    conn.commit()
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {"000001": 0.10})
    assert result["evaluated"] == 0


# ── DB 表自动创建 ───────────────────────────────────────


def test_creates_table_if_not_exists():
    db = Database(":memory:")
    ev = Evaluator(database=db)
    result = ev.evaluate("2026-07-17", {})
    assert result["evaluated"] == 0
    conn = db.get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_predictions'"
    ).fetchall()
    assert len(tables) == 1


# ── 验证 DB 写入 ────────────────────────────────────────


def test_updates_db_with_results():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.commit()
    ev = Evaluator(database=db)
    ev.evaluate("2026-07-17", {"000001": 0.05})
    row = conn.execute("SELECT actual_return, hit FROM agent_predictions WHERE id = 1").fetchone()
    assert row["actual_return"] == 0.05
    assert row["hit"] == 1


def test_updates_db_miss():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_predictions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER, created_at TEXT)"
    )
    conn.execute("INSERT INTO agent_predictions (code, date, verdict, confidence) VALUES ('000001', '2026-07-17', '推荐', 0.8)")
    conn.commit()
    ev = Evaluator(database=db)
    ev.evaluate("2026-07-17", {"000001": -0.05})
    row = conn.execute("SELECT actual_return, hit FROM agent_predictions WHERE id = 1").fetchone()
    assert row["actual_return"] == -0.05
    assert row["hit"] == 0
