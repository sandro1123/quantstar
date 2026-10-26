"""ST-15: daily_regime 状态落库与读 API 测试"""
import json
import pytest
from common.database import Database
from strategy_layer import StrategyLayer, StrategyStageContext
from strategy_layer.tables import init_strategy_tables


@pytest.fixture
def mock_db():
    db = Database(":memory:")
    db.init_db()
    init_strategy_tables(db.get_connection())
    yield db
    db.close()


def test_daily_regime_table_initialization(mock_db):
    """测试 daily_regime 表结构及初始化"""
    conn = mock_db.get_connection()
    cursor = conn.execute("PRAGMA table_info(daily_regime)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    assert "date" in columns
    assert "regime" in columns
    assert "confidence" in columns
    assert "avg_change" in columns
    assert "breadth" in columns
    assert "volume_score" in columns
    assert "composite_score" in columns
    assert "detail" in columns
    assert "created_at" in columns


def test_stage_ml_score_persists_daily_regime(mock_db, monkeypatch):
    """测试 _stage_ml_score 运行后 regime 结果幂等写入 daily_regime 表"""
    layer = StrategyLayer(db=mock_db)
    layer.start()

    # Mock market_regime 返回 bull 及指标
    def fake_market_regime(date):
        layer._last_regime_metrics = {
            "avg_change": 1.25,
            "breadth": 0.75,
            "volume_score": 1.1,
            "composite_score": 2.08,
        }
        return "bull"

    monkeypatch.setattr(layer, "market_regime", fake_market_regime)

    ctx = StrategyStageContext(date="2026-08-19")
    layer._stage_ml_score(ctx)

    assert ctx.regime == "bull"

    # 验证落库
    conn = mock_db.get_connection()
    row = conn.execute("SELECT * FROM daily_regime WHERE date = ?", ("2026-08-19",)).fetchone()
    assert row is not None
    assert row["date"] == "2026-08-19"
    assert row["regime"] == "bull"
    assert row["confidence"] == 1.0
    assert row["avg_change"] == 1.25
    assert row["breadth"] == 0.75
    assert row["volume_score"] == 1.1
    assert row["composite_score"] == 2.08
    assert row["detail"] is not None
    detail = json.loads(row["detail"])
    assert detail["composite_score"] == 2.08


def test_persist_daily_regime_idempotent_upsert(mock_db):
    """测试 _persist_daily_regime 相同日期重复写入时的 upsert 幂等性"""
    layer = StrategyLayer(db=mock_db)
    layer.start()

    layer._persist_daily_regime("2026-08-19", "neutral", 0.5, {"composite_score": 0.0})
    res1 = layer.get_daily_regime("2026-08-19")
    assert len(res1) == 1
    assert res1[0]["regime"] == "neutral"

    # 更新同日期为 bear
    layer._persist_daily_regime("2026-08-19", "bear", 1.0, {"composite_score": -2.1, "breadth": 0.2})
    res2 = layer.get_daily_regime("2026-08-19")
    assert len(res2) == 1
    assert res2[0]["regime"] == "bear"
    assert res2[0]["composite_score"] == -2.1


def test_get_daily_regime_reader_api(mock_db):
    """测试 get_daily_regime 读 API 支持单日字符串和日期范围查询"""
    layer = StrategyLayer(db=mock_db)
    layer.start()

    layer._persist_daily_regime("2026-08-10", "bull", 1.0, {"composite_score": 1.8})
    layer._persist_daily_regime("2026-08-11", "oscillating", 1.0, {"composite_score": 0.1})
    layer._persist_daily_regime("2026-08-12", "bear", 1.0, {"composite_score": -1.9})

    # 单日查询
    single = layer.get_daily_regime("2026-08-11")
    assert len(single) == 1
    assert single[0]["regime"] == "oscillating"

    # 范围查询
    range_res = layer.get_daily_regime(("2026-08-10", "2026-08-12"))
    assert len(range_res) == 3
    assert [r["date"] for r in range_res] == ["2026-08-10", "2026-08-11", "2026-08-12"]

    # 越界区间查询
    out_of_range = layer.get_daily_regime(("2026-08-15", "2026-08-20"))
    assert len(out_of_range) == 0
