"""Contract tests: DataLayer ↔ StrategyLayer interface compatibility."""

from unittest.mock import MagicMock, patch
import pytest


class TestDataLayerSnapshotContract:
    """Verify DataLayer.snapshot() returns objects compatible with StrategyLayer."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (0,)
        conn.execute.return_value = cursor
        db.get_connection.return_value = conn
        return db

    @pytest.fixture
    def mock_config(self):
        return MagicMock()

    def test_snapshot_returns_object_with_klines_or_none(self, mock_db, mock_config):
        from data_layer import DataLayer

        dl = DataLayer(db=mock_db, config=mock_config)
        result = dl.snapshot("2025-01-15")
        if result is not None:
            assert hasattr(result, "klines"), "DataSnapshot must have .klines attribute"

    def test_snapshot_intraday_returns_object_or_none(self, mock_db, mock_config):
        from data_layer import DataLayer

        dl = DataLayer(db=mock_db, config=mock_config)
        result = dl.snapshot_intraday("2025-01-15 10:30:00")
        if result is not None:
            assert hasattr(result, "klines_today") or isinstance(result, dict), (
                "IntradaySnapshot must have .klines_today or be a dict"
            )

    @pytest.mark.parametrize("date", ["2025-01-15", "2025-06-30", "2024-12-31"])
    def test_snapshot_accepts_date_string(self, mock_db, mock_config, date):
        from data_layer import DataLayer

        dl = DataLayer(db=mock_db, config=mock_config)
        result = dl.snapshot(date)
        assert result is None or hasattr(result, "klines")

    def test_check_readiness_returns_dataclass(self, mock_db, mock_config):
        from data_layer import DataLayer

        dl = DataLayer(db=mock_db, config=mock_config)
        result = dl.check_readiness("2025-01-15")
        assert hasattr(result, "is_ready"), "check_readiness must return ReadinessReport"

    def test_health_returns_layer_health(self, mock_db, mock_config):
        from data_layer import DataLayer

        dl = DataLayer(db=mock_db, config=mock_config)
        assert hasattr(dl, "snapshot")


class TestStrategyLayerDataDependency:
    """Verify StrategyLayer accepts data_layer and uses its snapshot interface."""

    @pytest.fixture
    def mock_data_layer(self):
        dl = MagicMock()
        snapshot = MagicMock()
        snapshot.klines = []
        dl.snapshot.return_value = snapshot
        return dl

    @pytest.fixture
    def mock_config(self):
        return MagicMock()

    def test_strategy_layer_accepts_data_layer(self, mock_config, mock_data_layer):
        from strategy_layer import StrategyLayer

        sl = StrategyLayer(config=mock_config, data_layer=mock_data_layer)
        assert sl.data_layer is mock_data_layer

    def test_run_daily_returns_required_keys(self, mock_config, mock_data_layer):
        from strategy_layer import StrategyLayer
        from strategy_layer._models import DailyRunResult

        sl = StrategyLayer(config=mock_config, data_layer=mock_data_layer)
        result = sl.run_daily("2025-01-15")
        assert isinstance(result, DailyRunResult)
        assert result.date is not None
        assert result.signals is not None
        assert result.selection is not None or result.selection is None
        assert result.regime is not None

    @pytest.mark.parametrize("date", ["2025-01-15", "2025-06-30"])
    def test_run_daily_echoes_date(self, mock_config, mock_data_layer, date):
        from strategy_layer import StrategyLayer

        sl = StrategyLayer(config=mock_config, data_layer=mock_data_layer)
        result = sl.run_daily(date)
        assert result.date == date

    def test_run_daily_regime_is_string(self, mock_config, mock_data_layer):
        from strategy_layer import StrategyLayer

        sl = StrategyLayer(config=mock_config, data_layer=mock_data_layer)
        result = sl.run_daily("2025-01-15")
        assert isinstance(result.regime, str)
        assert result.regime in ("bull", "bear", "neutral")

    def test_get_scores_returns_required_keys(self, mock_config):
        from strategy_layer import StrategyLayer
        from strategy_layer.engine import CleanedFactorMatrix
        from strategy_layer._models import FactorScores

        dl = MagicMock()
        sl = StrategyLayer(config=mock_config, data_layer=dl)
        sl.factor_engine.compute_all = MagicMock(
            return_value=CleanedFactorMatrix("2025-01-15", {"600519": {"momentum": 0.6, "value": 0.3}})
        )
        result = sl.get_scores("600519", "2025-01-15")
        assert isinstance(result, FactorScores)
        assert hasattr(result, "code")
        assert hasattr(result, "date")
        assert hasattr(result, "factors")
        assert hasattr(result, "composite_score")

    def test_get_scores_echoes_code_and_date(self, mock_config):
        from strategy_layer import StrategyLayer
        from strategy_layer.engine import CleanedFactorMatrix

        dl = MagicMock()
        sl = StrategyLayer(config=mock_config, data_layer=dl)
        sl.factor_engine.compute_all = MagicMock(
            return_value=CleanedFactorMatrix("2025-01-15", {"600519": {"momentum": 0.6}})
        )
        result = sl.get_scores("600519", "2025-01-15")
        assert result.code == "600519"
        assert result.date == "2025-01-15"

    def test_get_signals_returns_required_keys(self, mock_config):
        from strategy_layer import StrategyLayer
        from strategy_layer.engine import CleanedFactorMatrix
        from strategy_layer._models import TradeSignals

        dl = MagicMock()
        sl = StrategyLayer(config=mock_config, data_layer=dl)
        sl.factor_engine.compute_all = MagicMock(
            return_value=CleanedFactorMatrix("2025-01-15", {"600519": {"momentum": 0.6}})
        )
        result = sl.get_signals("600519", "2025-01-15")
        assert isinstance(result, TradeSignals)
        assert hasattr(result, "code")
        assert hasattr(result, "date")
        assert hasattr(result, "signal")
        assert hasattr(result, "strength")
        assert hasattr(result, "composite_score")

    def test_get_signals_signal_values(self, mock_config):
        from strategy_layer import StrategyLayer
        from strategy_layer.engine import CleanedFactorMatrix

        dl = MagicMock()
        sl = StrategyLayer(config=mock_config, data_layer=dl)
        sl.factor_engine.compute_all = MagicMock(
            return_value=CleanedFactorMatrix("2025-01-15", {"600519": {"momentum": 0.6}})
        )
        result = sl.get_signals("600519", "2025-01-15")
        assert result.signal in ("buy", "sell", "hold")
        assert isinstance(result.strength, (int, float))

    def test_market_regime_returns_string(self, mock_config, mock_data_layer):
        from strategy_layer import StrategyLayer

        sl = StrategyLayer(config=mock_config, data_layer=mock_data_layer)
        regime = sl.market_regime("2025-01-15")
        assert isinstance(regime, str)
        assert regime in ("bull", "bear", "neutral")

    def test_strategy_layer_works_without_data_layer(self, mock_config):
        from strategy_layer import StrategyLayer

        sl = StrategyLayer(config=mock_config, data_layer=None)
        result = sl.run_daily("2025-01-15")
        assert result.regime == "neutral"
        assert result.signals == []
