"""E2E: Ops checks all layers"""
import pytest
from unittest.mock import MagicMock
from common.database import Database
from ops_layer import OpsLayer
from ops_layer.core.models import LayerHealth, HealthCheck


class TestE2EOpsHealth:
    def test_ops_register_and_check_health(self):
        db = Database(":memory:")
        db.init_db()

        ops = OpsLayer(db=db)
        ops.start()

        green_layer = MagicMock()
        green_layer.health.return_value = LayerHealth(
            layer="data", status="green", checks=[], summary="ok"
        )
        ops.register_layers({"data": green_layer})

        report = ops.check_health()

        assert report.overall_status in ("green", "yellow", "red")
        assert len(report.layers) > 0

    def test_each_layer_has_health_entry(self):
        db = Database(":memory:")
        db.init_db()
        ops = OpsLayer(db=db)
        ops.start()

        mock_data = MagicMock()
        mock_data.health.return_value = LayerHealth(
            layer="data", status="green", checks=[], summary="ok"
        )

        mock_strategy = MagicMock()
        mock_strategy.health.return_value = LayerHealth(
            layer="strategy", status="green", checks=[], summary="ok"
        )

        ops.register_layers({"data": mock_data, "strategy": mock_strategy})
        report = ops.check_health()

        layer_names = [lh.layer for lh in report.layers]
        assert "data" in layer_names
        assert "strategy" in layer_names

    def test_ops_health_status_values(self):
        db = Database(":memory:")
        db.init_db()
        ops = OpsLayer(db=db)
        ops.start()

        green_layer = MagicMock()
        green_layer.health.return_value = LayerHealth(
            layer="a", status="green", checks=[], summary="ok"
        )

        ops.register_layers({"a": green_layer})
        report = ops.check_health()

        assert report.overall_status == "green"
        assert report.layers[0].status == "green"

    def test_ops_health_with_failing_layer(self):
        db = Database(":memory:")
        db.init_db()
        ops = OpsLayer(db=db)
        ops.start()

        good_layer = MagicMock()
        good_layer.health.return_value = LayerHealth(
            layer="good", status="green", checks=[], summary="ok"
        )

        bad_layer = MagicMock()
        bad_layer.health.side_effect = RuntimeError("connection lost")

        ops.register_layers({"good": good_layer, "bad": bad_layer})
        report = ops.check_health()

        assert report.overall_status == "red"
        assert len(report.needs_attention) > 0

    @pytest.mark.parametrize(
        ("layer", "expected_summary"),
        [(None, "层未初始化"), (object(), "缺少健康检查接口")],
    )
    def test_unavailable_layer_is_red(self, layer, expected_summary):
        db = Database(":memory:")
        db.init_db()
        ops = OpsLayer(db=db)
        ops.start()

        ops.register_layers({"bare": layer})
        report = ops.check_health()

        assert report.overall_status == "red"
        bare = [lh for lh in report.layers if lh.layer == "bare"]
        assert len(bare) == 1
        assert bare[0].status == "red"
        assert expected_summary in bare[0].summary

    def test_invalid_health_contract_is_red(self):
        db = Database(":memory:")
        db.init_db()
        ops = OpsLayer(db=db)
        ops.start()

        invalid_layer = MagicMock()
        invalid_layer.health.return_value = None
        ops.register_layers({"invalid": invalid_layer})
        report = ops.check_health()

        assert report.overall_status == "red"
        assert report.layers[0].status == "red"
        assert "expected LayerHealth" in report.layers[0].summary
