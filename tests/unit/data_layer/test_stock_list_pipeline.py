"""Tests for StockListPipeline — stock_info 全市场同步"""
from unittest.mock import MagicMock, patch

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.stock_list import StockListPipeline


def test_stock_list_pipeline_upserts_records():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            industry TEXT DEFAULT '',
            market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    pipeline = StockListPipeline()
    mock_data = [
        {"code": "920065", "name": "测试北交所A"},
        {"code": "920258", "name": "测试北交所B"},
        {"code": "688825", "name": "测试科创板"},
        {"code": "001232", "name": "测试深市"},
        {"code": "920189", "name": "测试北交所C"},
    ]

    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ):
        result = pipeline.run(PipelineInput(date="2026-08-12", db=db))

    assert result.status == "success"
    assert result.records_written == 5

    for rec in mock_data:
        row = conn.execute(
            "SELECT name, market FROM stock_info WHERE code = ?", (rec["code"],)
        ).fetchone()
        assert row is not None, f"{rec['code']} not found in stock_info"
        assert row["name"] == rec["name"]

    db.close()


def test_stock_list_pipeline_fails_on_empty_source():
    db = Database(":memory:")
    pipeline = StockListPipeline()

    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=[],
    ):
        result = pipeline.run(PipelineInput(date="2026-08-12", db=db))

    assert result.status == "failed"
    assert "返回空" in result.errors[0]
    db.close()


def test_stock_list_pipeline_fails_on_source_exception():
    db = Database(":memory:")
    pipeline = StockListPipeline()

    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        side_effect=Exception("网络错误"),
    ):
        result = pipeline.run(PipelineInput(date="2026-08-12", db=db))

    assert result.status == "failed"
    assert "网络错误" in result.errors[0]
    db.close()

# ---------------------------------------------------------------------------
# DL-XX: 名录 diff 退市检测
# ---------------------------------------------------------------------------

def _make_db_with_stock_info():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            industry TEXT DEFAULT '',
            market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            delist_date TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return db, conn


def test_delist_diff_marks_missing_stocks():
    """本次名录缺失的活跃股 → is_active=0 + delist_date=input.date"""
    db, conn = _make_db_with_stock_info()
    # 预置 3 只活跃股
    for code, name in [("000001", "平安银行"), ("000002", "万科A"), ("600000", "浦发银行")]:
        conn.execute(
            "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
            (code, name),
        )
    conn.commit()

    pipeline = StockListPipeline()
    # 本次名录只有 2 只 — 600000 消失
    mock_data = [
        {"code": "000001", "name": "平安银行"},
        {"code": "000002", "name": "万科A"},
    ]
    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ):
        result = pipeline.run(PipelineInput(date="2026-08-28", db=db))

    assert result.status == "success"
    assert result.metadata["delist_marked"] == 1
    row = conn.execute(
        "SELECT is_active, delist_date FROM stock_info WHERE code = '600000'"
    ).fetchone()
    assert row["is_active"] == 0
    assert row["delist_date"] == "20260828"
    # 在场股不受影响
    row2 = conn.execute(
        "SELECT is_active FROM stock_info WHERE code = '000001'"
    ).fetchone()
    assert row2["is_active"] == 1
    db.close()


def test_delist_diff_skips_on_source_truncation():
    """缺失数超阈值 max(50, 5%) → 判定源残缺, 不标记"""
    db, conn = _make_db_with_stock_info()
    # 预置 100 只活跃股
    for i in range(100):
        conn.execute(
            "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
            (f"{i:06d}", f"股票{i}"),
        )
    conn.commit()

    pipeline = StockListPipeline()
    # 本次名录只返回 90 只 — 缺失 10 只, 但阈值 max(50, 5)=50, 10<50 不触发
    # 改用: 返回 40 只 → 缺失 60 > 50 → 触发跳过
    mock_data = [{"code": f"{i:06d}", "name": f"股票{i}"} for i in range(40)]
    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ):
        result = pipeline.run(PipelineInput(date="2026-08-28", db=db))

    assert result.status == "success"
    assert result.metadata["delist_marked"] == 0
    assert any("delist_diff_skipped" in e for e in result.errors)
    # 所有股票仍 active
    n_active = conn.execute(
        "SELECT COUNT(*) FROM stock_info WHERE is_active = 1"
    ).fetchone()[0]
    assert n_active == 100
    db.close()


def test_delist_diff_no_missing():
    """名录完整 → 0 标记, 无 delist 相关 error"""
    db, conn = _make_db_with_stock_info()
    conn.execute(
        "INSERT INTO stock_info (code, name, is_active) VALUES ('000001', '平安银行', 1)"
    )
    conn.commit()

    pipeline = StockListPipeline()
    mock_data = [{"code": "000001", "name": "平安银行"}]
    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ):
        result = pipeline.run(PipelineInput(date="2026-08-28", db=db))

    assert result.status == "success"
    assert result.metadata["delist_marked"] == 0
    assert not any("delist" in e for e in result.errors)
    db.close()


def test_delist_diff_tushare_refines_date():
    """tushare delist 接口可用时精确化 delist_date"""
    db, conn = _make_db_with_stock_info()
    conn.execute(
        "INSERT INTO stock_info (code, name, is_active) VALUES ('600000', '浦发银行', 1)"
    )
    conn.commit()

    pipeline = StockListPipeline()
    mock_data = [{"code": "000001", "name": "平安银行"}]
    ts_delist = [{"code": "600000", "name": "浦发银行", "delist_date": "20260815"}]

    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ), patch(
        "data_layer.sources.tushare_source.TushareSourceAdapter.is_available",
        return_value=True,
    ), patch(
        "data_layer.sources.tushare_source.TushareSourceAdapter.get_data",
        side_effect=lambda dt, p: ts_delist if dt == "delist" else [],
    ):
        result = pipeline.run(PipelineInput(date="2026-08-28", db=db))

    assert result.status == "success"
    assert result.metadata["delist_marked"] == 1
    row = conn.execute(
        "SELECT delist_date FROM stock_info WHERE code = '600000'"
    ).fetchone()
    # tushare 精确日期覆盖 input.date
    assert row["delist_date"] == "20260815"
    db.close()


def test_delist_diff_excludes_bse_prefixes():
    """D3 案: 北交所 8/4/92 头不计入缺失集 — 源不覆盖, 防顶翻安全闸误全跳过。"""
    db, conn = _make_db_with_stock_info()
    # 在场沪深 2 只 + 沪深死股 1 只 + 北交所旧码 60 只 (83 头, 全不在名录)
    for code, name in [("000001", "平安银行"), ("600000", "浦发银行"),
                       ("000024", "招商地产")]:
        conn.execute(
            "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
            (code, name),
        )
    for i in range(60):
        conn.execute(
            "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
            (f"83{i:04d}", f"北旧{i}"),
        )
    conn.commit()

    pipeline = StockListPipeline()
    mock_data = [{"code": "000001", "name": "平安银行"},
                 {"code": "600000", "name": "浦发银行"}]
    with patch(
        "data_layer.sources.akshare_sina_source.AkshareSinaAdapter.get_data",
        return_value=mock_data,
    ):
        result = pipeline.run(PipelineInput(date="2026-09-11", db=db))

    # 缺失判定只含 000024 (沪深) — 60 只 83 头不计入, 阈值不误触
    assert result.metadata["delist_marked"] == 1
    row = conn.execute(
        "SELECT is_active FROM stock_info WHERE code = '000024'").fetchone()
    assert row[0] == 0
    n_bse_active = conn.execute(
        "SELECT COUNT(*) FROM stock_info WHERE code LIKE '83%' AND is_active = 1"
    ).fetchone()[0]
    assert n_bse_active == 60
    db.close()
