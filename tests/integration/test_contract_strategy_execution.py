"""Contract tests: StrategyLayer ↔ ExecutionLayer interface compatibility."""

from unittest.mock import MagicMock, patch
import pytest


class TestExecutionLayerConstructorContract:
    """Verify ExecutionLayer accepts all required dependencies."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        return MagicMock()

    @pytest.fixture
    def mock_data_layer(self):
        return MagicMock()

    @pytest.fixture
    def mock_strategy_layer(self):
        return MagicMock()

    def test_execution_layer_accepts_all_deps(
        self, mock_db, mock_config, mock_data_layer, mock_strategy_layer
    ):
        from execution_layer import ExecutionLayer

        el = ExecutionLayer(
            db=mock_db, config=mock_config,
            data_layer=mock_data_layer, strategy_layer=mock_strategy_layer,
        )
        assert el.db is mock_db
        assert el.config is mock_config
        assert el.data_layer is mock_data_layer
        assert el.strategy_layer is mock_strategy_layer

    def test_execution_layer_accepts_none_defaults(self):
        from execution_layer import ExecutionLayer

        el = ExecutionLayer()
        assert el.db is not None  # defaults to Database()
        assert el.config is None
        assert el.data_layer is None
        assert el.strategy_layer is None


class TestPortfolioSnapshotContract:
    """Verify ExecutionLayer.get_portfolio_snapshot() returns required structure."""

    @pytest.fixture
    def execution_layer(self):
        from execution_layer import ExecutionLayer
        el = ExecutionLayer(
            db=MagicMock(), config=MagicMock(),
            data_layer=MagicMock(), strategy_layer=MagicMock(),
        )
        el.stock_pool = MagicMock()
        el.stock_pool.count_by_status.return_value = {
            "active": 0, "pending_entry": 0, "exit_pending": 0, "watch": 0, "exited": 0
        }
        el.stock_pool.get_by_status.return_value = []
        el.stock_pool.get_available_cash.return_value = 300000.0
        return el

    def test_get_portfolio_snapshot_returns_dict(self, execution_layer):
        from execution_layer.stats.display import PortfolioSnapshot
        result = execution_layer.get_portfolio_snapshot()
        assert isinstance(result, PortfolioSnapshot)

    def test_get_portfolio_snapshot_has_required_keys(self, execution_layer):
        result = execution_layer.get_portfolio_snapshot()
        assert hasattr(result, "active")
        assert hasattr(result, "pending_entry")
        assert hasattr(result, "pending_exit")
        assert hasattr(result, "watch")
        assert hasattr(result, "exited")

    def test_get_portfolio_snapshot_values_are_integers(self, execution_layer):
        result = execution_layer.get_portfolio_snapshot()
        for key in ("active", "pending_entry", "pending_exit", "watch", "exited"):
            assert isinstance(getattr(result, key), int), f"{key} must be int, got {type(getattr(result, key))}"

    def test_get_portfolio_snapshot_non_negative(self, execution_layer):
        result = execution_layer.get_portfolio_snapshot()
        for key in ("active", "pending_entry", "pending_exit", "watch", "exited"):
            assert getattr(result, key) >= 0, f"{key} must be >= 0"


class TestTodayActionsContract:
    """Verify ExecutionLayer.get_today_actions() returns list[dict]."""

    @pytest.fixture
    def execution_layer(self):
        from execution_layer import ExecutionLayer
        return ExecutionLayer(
            db=MagicMock(), config=MagicMock(),
            data_layer=MagicMock(), strategy_layer=MagicMock(),
        )

    def test_get_today_actions_returns_list(self, execution_layer):
        from execution_layer.stats.display import ActionFeed
        result = execution_layer.get_today_actions()
        assert isinstance(result, ActionFeed)

    def test_get_today_actions_items_are_dicts(self, execution_layer):
        result = execution_layer.get_today_actions()
        for item in result.actions:
            assert isinstance(item, dict), "Each action must be a dict"


class TestExecutionLayerHealthContract:
    """Verify ExecutionLayer.health() exists and is callable."""

    @pytest.fixture
    def execution_layer(self):
        from execution_layer import ExecutionLayer
        return ExecutionLayer(
            db=MagicMock(), config=MagicMock(),
            data_layer=MagicMock(), strategy_layer=MagicMock(),
        )

    def test_health_method_exists(self, execution_layer):
        assert callable(getattr(execution_layer, "health", None))

    def test_health_returns_object(self, execution_layer):
        health = execution_layer.health()
        assert health is not None
        assert hasattr(health, "layer") or isinstance(health, dict)

    def test_health_has_status(self, execution_layer):
        health = execution_layer.health()
        if hasattr(health, "status"):
            assert health.status in ("green", "yellow", "red")
        elif isinstance(health, dict):
            assert "status" in health
