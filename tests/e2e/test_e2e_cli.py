"""E2E: CLI commands work"""
import pytest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from cli.main import cli
from ops_layer.core.models import PipelineInfo, PipelineRunResult


class TestE2ECLI:
    def test_version_command(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["version"])

        assert result.exit_code == 0
        assert "QuantStar" in result.output

    @patch("bootstrap.bootstrap_app")
    def test_health_command(self, mock_bootstrap):
        mock_app = MagicMock()
        mock_report = MagicMock()
        mock_report.overall_status = "green"
        mock_report.layers = [MagicMock(status="green", layer="data", summary="ok")]
        mock_app.ops.check_health.return_value = mock_report
        mock_bootstrap.return_value = mock_app

        runner = CliRunner()
        result = runner.invoke(cli, ["health"])

        assert result.exit_code == 0
        assert "健康检查" in result.output

    @patch("bootstrap.bootstrap_app")
    def test_status_command(self, mock_bootstrap):
        mock_app = MagicMock()
        mock_bootstrap.return_value = mock_app

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "系统状态" in result.output

    @patch("bootstrap.bootstrap_app")
    @patch("cli.main._app_cache", None)
    def test_status_command_failure(self, mock_bootstrap):
        mock_bootstrap.side_effect = RuntimeError("bootstrap failed")

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])

        assert result.exit_code != 0

    @patch("cli.main._app_cache", None)
    @patch("bootstrap.bootstrap_app")
    def test_pipeline_list_uses_dataclass_contract(self, mock_bootstrap):
        mock_app = MagicMock()
        mock_app.ops.list_pipelines.return_value = [
            PipelineInfo(name="daily_data_pipeline", schedule="0 18 * * 1-5", depends_on=[])
        ]
        mock_bootstrap.return_value = mock_app

        result = CliRunner().invoke(cli, ["pipeline", "list"])

        assert result.exit_code == 0
        assert "daily_data_pipeline" in result.output
        assert "0 18 * * 1-5" in result.output

    @patch("cli.main._app_cache", None)
    @patch("bootstrap.bootstrap_app")
    def test_pipeline_dag_uses_dataclass_contract(self, mock_bootstrap):
        mock_app = MagicMock()
        mock_app.ops.list_pipelines.return_value = [
            PipelineInfo(name="factor_update", schedule="2", depends_on=["daily_data_pipeline"])
        ]
        mock_bootstrap.return_value = mock_app

        result = CliRunner().invoke(cli, ["pipeline", "dag"])

        assert result.exit_code == 0
        assert "factor_update" in result.output
        assert "daily_data_pipeline" in result.output

    @patch("cli.main._app_cache", None)
    @patch("bootstrap.bootstrap_app")
    def test_pipeline_run_failure_exits_nonzero(self, mock_bootstrap):
        mock_app = MagicMock()
        mock_app.ops.run_pipeline.return_value = PipelineRunResult(
            name="daily_data_pipeline", status="failed", error="source unavailable"
        )
        mock_bootstrap.return_value = mock_app

        result = CliRunner().invoke(cli, ["pipeline", "run", "daily_data_pipeline"])

        assert result.exit_code != 0
        assert "source unavailable" in result.output

    def test_cli_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "QuantStar" in result.output
