"""Tests for SQL injection prevention in Data Layer"""
import pytest
from common.utils import validate_identifier
from data_layer import DataLayer
from data_layer.pit import PITQuery, PITDataProvider
from common.database import Database


class TestValidateIdentifier:
    def test_valid_identifiers(self):
        assert validate_identifier("fwd_return_20d") is True
        assert validate_identifier("close") is True
        assert validate_identifier("daily_klines") is True
        assert validate_identifier("stock_financials") is True
        assert validate_identifier("_valid_underscore") is True
        assert validate_identifier("a1b2c3") is True

    def test_invalid_identifiers(self):
        assert validate_identifier("") is False
        assert validate_identifier("table; DROP") is False
        assert validate_identifier("col-- ") is False
        assert validate_identifier("col' OR 1=1") is False
        assert validate_identifier("") is False
        assert validate_identifier("123abc") is False
        assert validate_identifier("col name") is False


class TestComputeFwdReturnsSQLInjection:
    def test_rejects_invalid_label_column(self):
        dl = DataLayer(db=Database(":memory:"))
        result = dl.compute_fwd_returns(label_column="col; DROP TABLE factor_daily--")
        assert result is None

    def test_rejects_invalid_label_column_get_training(self):
        dl = DataLayer(db=Database(":memory:"))
        result = dl.get_training_dataset(label_column="col; DROP TABLE factor_daily--")
        assert result is None


class TestPITSQLInjection:
    def test_rejects_injection_table_name(self):
        db = Database(":memory:")
        provider = PITDataProvider(db)
        query = PITQuery(
            code="000001",
            as_of_date="2026-07-17",
            table="daily_klines; DROP TABLE users--",
            columns=["close"],
        )
        with pytest.raises(ValueError, match="Invalid table name"):
            provider.query(query)

    def test_rejects_injection_column_name(self):
        db = Database(":memory:")
        provider = PITDataProvider(db)
        query = PITQuery(
            code="000001",
            as_of_date="2026-07-17",
            table="daily_klines",
            columns=["close; DROP TABLE users--"],
        )
        with pytest.raises(ValueError, match="Invalid column name"):
            provider.query(query)


class TestLoaderForwardReturnSQLInjection:
    def test_forward_return_safe(self):
        """horizon=999 returns None (not in horizon_map) and doesn't crash"""
        from data_layer._loader import DataLoader
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
        conn.commit()
        loader = DataLoader(db)
        result = loader.forward_return("000001", "2026-07-17", horizon=999)
        assert result is None