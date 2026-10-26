"""tests/unit/data_layer/test_akshare_sources.py
AkShareSourceAdapter 涨跌停/财务接口字段映射单元测试
"""
import sys
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

# Resolve qs project root on device
_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))


def _make_akshare_mock():
    """Create a mock akshare module with stock_zt_pool_em etc."""
    import pandas as pd
    ak = MagicMock()

    up_df = pd.DataFrame({
        "代码": ["000001", "600519"],
        "名称": ["平安银行", "贵州茅台"],
        "最新价": [12.50, 1680.00],
        "涨跌幅": [10.02, 10.01],
        "连板数": [1, 2],
    })
    ak.stock_zt_pool_em.return_value = up_df

    down_df = pd.DataFrame({
        "代码": ["000002"],
        "名称": ["万科A"],
        "最新价": [8.30],
        "涨跌幅": [-10.03],
        "连续跌停": [3],
    })
    ak.stock_zt_pool_dtgc_em.return_value = down_df

    fin_df = pd.DataFrame({
        "日期": ["2026-06-30"],
        "摊薄每股收益(元)": [1.23],
        "净资产收益率(%)": [15.6],
        # 注意: akshare 返回单位是"元"不是"万元", 列名也不同
        "主营业务利润(元)": [50000.0 * 1e4],  # 50000万元 = 5e8元
        "扣除非经常性损益后的净利润(元)": [8000.0 * 1e4],  # 8000万元 = 8e7元
        "总资产(元)": [200000.0 * 1e4],  # 200000万元 = 2e9元
        "资产负债率(%)": [45.0],  # 45% → total_liabilities = 200000 * 0.45 = 90000万元
        "销售毛利率(%)": [30.5],
        "销售净利率(%)": [16.0],
    })
    ak.stock_financial_analysis_indicator.return_value = fin_df

    return ak


def test_capabilities_include_new_types():
    """AkShareSourceAdapter.capabilities 应包含 limit_up, limit_down, financial"""
    from data_layer.sources.akshare_source import AkShareSourceAdapter
    adapter = AkShareSourceAdapter()
    caps = adapter.capabilities
    assert "limit_up" in caps, f"limit_up not in {caps}"
    assert "limit_down" in caps, f"limit_down not in {caps}"
    assert "financial" in caps, f"financial not in {caps}"


def test_get_limit_up_pool_field_mapping():
    """涨停池字段映射: 代码→code, 名称→name, 最新价→price, 涨跌幅→change_pct, 连板数→count"""
    mock_ak = _make_akshare_mock()
    saved = sys.modules.get("akshare")
    sys.modules["akshare"] = mock_ak
    try:
        from data_layer.sources.akshare_source import AkShareSourceAdapter
        adapter = AkShareSourceAdapter()
        rows = adapter.get_data("limit_up", {"date": "2026-08-13"})
        assert rows is not None, "limit_up returned None"
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
        r0 = rows[0]
        assert r0["code"] == "000001", f"code mismatch: {r0['code']}"
        assert r0["name"] == "平安银行", f"name mismatch: {r0['name']}"
        assert r0["limit_type"] == "up", f"limit_type should be 'up': {r0['limit_type']}"
        assert r0["price"] == 12.50, f"price mismatch: {r0['price']}"
        assert r0["change_pct"] == 10.02, f"change_pct mismatch: {r0['change_pct']}"
        assert r0["count"] == 1, f"count mismatch: {r0['count']}"
        assert r0["date"] == "2026-08-13", f"date mismatch: {r0['date']}"
    finally:
        if saved is not None:
            sys.modules["akshare"] = saved
        else:
            sys.modules.pop("akshare", None)


def test_get_limit_down_pool_field_mapping():
    """跌停池字段映射: 代码→code, 名称→name, 最新价→price, 涨跌幅→change_pct, 连续跌停→count"""
    mock_ak = _make_akshare_mock()
    saved = sys.modules.get("akshare")
    sys.modules["akshare"] = mock_ak
    try:
        from data_layer.sources.akshare_source import AkShareSourceAdapter
        adapter = AkShareSourceAdapter()
        rows = adapter.get_data("limit_down", {"date": "2026-08-13"})
        assert rows is not None, "limit_down returned None"
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
        r0 = rows[0]
        assert r0["code"] == "000002", f"code mismatch: {r0['code']}"
        assert r0["name"] == "万科A", f"name mismatch: {r0['name']}"
        assert r0["limit_type"] == "down", f"limit_type should be 'down': {r0['limit_type']}"
        assert r0["price"] == 8.30, f"price mismatch: {r0['price']}"
        assert r0["change_pct"] == -10.03, f"change_pct mismatch: {r0['change_pct']}"
        assert r0["count"] == 3, f"count mismatch: {r0['count']}"
    finally:
        if saved is not None:
            sys.modules["akshare"] = saved
        else:
            sys.modules.pop("akshare", None)


def test_get_financial_field_mapping():
    """财务指标字段映射: 摊薄每股收益→eps, 净资产收益率→roe, 主营业务收入→revenue, etc."""
    mock_ak = _make_akshare_mock()
    saved = sys.modules.get("akshare")
    sys.modules["akshare"] = mock_ak
    try:
        from data_layer.sources.akshare_source import AkShareSourceAdapter
        adapter = AkShareSourceAdapter()
        rec = adapter.get_data("financial", {"code": "000001", "date": "2026-08-13"})
        assert rec is not None, "financial returned None"
        assert rec["code"] == "000001", f"code mismatch: {rec['code']}"
        assert rec["eps"] == 1.23, f"eps mismatch: {rec['eps']}"
        assert rec["roe"] == 15.6, f"roe mismatch: {rec['roe']}"
        assert rec["revenue"] == 50000.0, f"revenue mismatch: {rec['revenue']}"
        assert rec["profit"] == 8000.0, f"profit mismatch: {rec['profit']}"
        assert rec["total_assets"] == 200000.0, f"total_assets mismatch: {rec['total_assets']}"
        assert rec["total_liabilities"] == 90000.0, f"total_liabilities mismatch: {rec['total_liabilities']}"
        assert rec["net_assets"] == 110000.0, f"net_assets mismatch: {rec['net_assets']}"
        assert rec["gross_margin"] == 30.5, f"gross_margin mismatch: {rec['gross_margin']}"
        assert rec["net_margin"] == 16.0, f"net_margin mismatch: {rec['net_margin']}"
    finally:
        if saved is not None:
            sys.modules["akshare"] = saved
        else:
            sys.modules.pop("akshare", None)


class _FakeDB:
    """Wrapper so sqlite3.Connection has get_connection() like Database."""
    def __init__(self, conn):
        self._conn = conn
    def get_connection(self):
        return self._conn


def test_limit_up_down_pipeline_uses_registry():
    """LimitUpDownPipeline 应通过 SourceRegistry 拉取涨停/跌停数据并写入 stock_limit_up_down 表"""
    from data_layer.pipelines.limit_up_down import LimitUpDownPipeline
    from data_layer.pipelines.base import PipelineInput

    raw_db = sqlite3.connect(":memory:")
    raw_db.row_factory = sqlite3.Row
    raw_db.execute("""
        CREATE TABLE stock_limit_up_down (
            code TEXT, date TEXT, name TEXT, limit_type TEXT,
            price REAL, change_pct REAL, count INTEGER,
            PRIMARY KEY (code, date, limit_type)
        )
    """)
    raw_db.commit()
    db = _FakeDB(raw_db)

    class FakeSource:
        name = "fake_akshare"
        capabilities = {"limit_up", "limit_down", "financial"}
        def is_available(self): return True
        def get_data(self, data_type, params):
            if data_type == "limit_up":
                return [{"code": "000001", "name": "平安银行", "date": params.get("date",""),
                         "limit_type": "up", "price": 12.5, "change_pct": 10.02, "count": 1}]
            elif data_type == "limit_down":
                return [{"code": "000002", "name": "万科A", "date": params.get("date",""),
                         "limit_type": "down", "price": 8.3, "change_pct": -10.03, "count": 3}]
            return None

    class FakeRegistry:
        def get_sources_for(self, data_type):
            if data_type in ("limit_up", "limit_down"):
                return [FakeSource()]
            return []

    pipe = LimitUpDownPipeline(db=db, source_registry=FakeRegistry())
    result = pipe.run(PipelineInput(date="2026-08-13", db=db))
    assert result.status == "success", f"status should be success: {result.status}"
    assert result.records_written == 2, f"records should be 2: {result.records_written}"

    rows = raw_db.execute(
        "SELECT * FROM stock_limit_up_down WHERE date=? ORDER BY limit_type", ("2026-08-13",)
    ).fetchall()
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    up = [r for r in rows if r["limit_type"] == "limit_up"][0]
    down = [r for r in rows if r["limit_type"] == "limit_down"][0]
    assert up["code"] == "000001" and up["price"] == 12.5
    assert down["code"] == "000002" and down["change_pct"] == -10.03
    raw_db.close()


def test_limit_up_down_pipeline_no_sources():
    """无可用源时应返回 failed/partial, 不应崩溃"""
    from data_layer.pipelines.limit_up_down import LimitUpDownPipeline
    from data_layer.pipelines.base import PipelineInput

    raw_db = sqlite3.connect(":memory:")
    raw_db.row_factory = sqlite3.Row
    raw_db.execute("""
        CREATE TABLE stock_limit_up_down (
            code TEXT, date TEXT, name TEXT, limit_type TEXT,
            price REAL, change_pct REAL, count INTEGER,
            PRIMARY KEY (code, date, limit_type)
        )
    """)
    raw_db.commit()
    db = _FakeDB(raw_db)

    class EmptyRegistry:
        def get_sources_for(self, data_type):
            return []

    pipe = LimitUpDownPipeline(db=db, source_registry=EmptyRegistry())
    result = pipe.run(PipelineInput(date="2026-08-13", db=db))
    assert result.records_written == 0, f"records should be 0: {result.records_written}"
    assert result.status in ("failed", "partial"), f"status should be failed/partial: {result.status}"
    raw_db.close()
