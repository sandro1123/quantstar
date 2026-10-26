"""S2 F2.4 — 财务日期 ISO 统一回归测试"""
import sqlite3

from data_layer._loader import DataLoader, _norm_fin_date
from data_layer.pipelines.financial import _norm_iso_date


class TestNormFn:
    def test_pipeline_compact_to_iso(self):
        assert _norm_iso_date("20260807") == "2026-08-07"
        assert _norm_iso_date("2026-08-07") == "2026-08-07"
        assert _norm_iso_date("202608") == "202608"
        assert _norm_iso_date("") == ""

    def test_loader_compact_to_iso(self):
        assert _norm_fin_date("20260807") == "2026-08-07"
        assert _norm_fin_date("2026-06-30") == "2026-06-30"
        assert _norm_fin_date("invalid") == "invalid"


class TestLoaderDefensiveNorm:
    def _db_with_compact(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE stock_financials (
                code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL,
                profit REAL, total_assets REAL, total_liabilities REAL,
                net_assets REAL, operating_cash_flow REAL,
                gross_margin REAL, net_margin REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.execute(
            "INSERT INTO stock_financials (code, date, eps, revenue, profit) VALUES ('000001', '20260630', 1.2, 100, 20)"
        )
        conn.commit()
        return conn

    def test_get_financials_normalizes_compact_date(self):
        conn = self._db_with_compact()
        loader = DataLoader.__new__(DataLoader)
        fins = loader._get_financials(conn, "20260630")
        assert len(fins) == 1
        assert fins[0].date == "2026-06-30"
