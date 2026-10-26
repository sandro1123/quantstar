"""BT-11 ②: CLI backtest --html 接线测试

HTMLReportBuilder 接入 CLI backtest 命令 — 回测结果产出用户可见的 HTML 报告。
CliRunner + mock _lazy_bootstrap, 不碰真实 bootstrap / 生产库。
"""
from unittest.mock import patch

from click.testing import CliRunner

from backtest_layer.core.models import BacktestReport
from cli.main import cli


def _report():
    return BacktestReport(
        mode="hold", date_range=("2026-08-18", "2026-08-18"),
        total_return=0.05, annual_return=0.10, sharpe=1.2,
        max_drawdown=0.03, win_rate=0.6, total_trades=10,
        benchmark_return=0.0, excess_return=0.05,
    )


class _FakeApp:
    """app.backtest.run 返回固定报告"""

    def __init__(self):
        self.backtest = self

    def run(self, bt_input):
        return _report()


class TestBacktestHtml:
    def test_html_flag_writes_report(self, tmp_path):
        out = tmp_path / "report.html"
        runner = CliRunner()
        with patch("cli.main._lazy_bootstrap", return_value=_FakeApp()):
            result = runner.invoke(cli, ["backtest", "--date", "2026-08-18",
                                         "--code", "000001", "--html",
                                         "--output", str(out)])
        assert result.exit_code == 0
        assert out.exists()
        html = out.read_text(encoding="utf-8")
        assert "<html" in html.lower()
        assert "HOLD" in html.upper()

    def test_no_html_flag_no_file(self, tmp_path):
        runner = CliRunner()
        with patch("cli.main._lazy_bootstrap", return_value=_FakeApp()):
            result = runner.invoke(cli, ["backtest", "--date", "2026-08-18",
                                         "--code", "000001"])
        assert result.exit_code == 0
        assert "HTML 报告" not in result.output
