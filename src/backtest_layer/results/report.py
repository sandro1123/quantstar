"""backtest_layer/results/report.py — HTML 报告生成器

生成包含净值曲线、指标汇总、交易记录的 HTML 报告。
零外部依赖，纯 Python 字符串模板。
"""

import json
import os
from datetime import datetime


class ReportStore:
    def __init__(self, base_dir: str = "reports"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def save(self, report, filename: str | None = None) -> str:
        if filename is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"backtest_{report.mode}_{ts}.json"
        path = os.path.join(self.base_dir, filename)
        data = {
            "mode": report.mode,
            "date_range": report.date_range,
            "total_return": report.total_return,
            "annual_return": report.annual_return,
            "sharpe": report.sharpe,
            "max_drawdown": report.max_drawdown,
            "win_rate": report.win_rate,
            "total_trades": report.total_trades,
            "benchmark_return": report.benchmark_return,
            "excess_return": report.excess_return,
            "equity_curve": report.equity_curve,
            "trade_log": report.trade_log,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def load(self, filename: str) -> dict:
        path = os.path.join(self.base_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


class HTMLReportBuilder:
    @staticmethod
    def _pct(value, na="—"):
        """BT-04: None 安全百分比格式 — 不可算时显式占位, 绝不按 0 渲染。"""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:.2%}"
        return na

    @staticmethod
    def _num2(value, na="—"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:.2f}"
        return na

    @staticmethod
    def build(report, benchmark_comparison: dict | None = None) -> str:
        title = f"回测报告 — {report.mode.upper()}"
        date_range = f"{report.date_range[0]} ~ {report.date_range[1]}"
        metrics_rows = HTMLReportBuilder._metrics_section(report)
        trade_rows = HTMLReportBuilder._trade_section(report)
        # BT-04: 未显式传 comparison 时自动取报告自带的基准对比段
        comparison = benchmark_comparison or getattr(report, "benchmark_comparison", None) or None
        benchmark_section = HTMLReportBuilder._benchmark_section(comparison) if comparison else ""
        json.dumps(report.equity_curve) if report.equity_curve else "[]"
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 20px; background: #f5f5f5; }}
.container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
h2 {{ color: #555; margin-top: 30px; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }}
.metric-card {{ background: #f8f9fa; padding: 15px; border-radius: 6px; text-align: center; }}
.metric-value {{ font-size: 24px; font-weight: bold; color: #007bff; }}
.metric-label {{ font-size: 12px; color: #888; margin-top: 5px; }}
.positive {{ color: #28a745; }}
.negative {{ color: #dc3545; }}
table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f8f9fa; font-weight: 600; }}
.verdict {{ background: #e8f5e9; padding: 15px; border-radius: 6px; margin: 20px 0; font-size: 16px; }}
</style>
</head>
<body>
<div class="container">
<h1>{title}</h1>
<p style="color:#888;">{date_range}</p>

<h2>核心指标</h2>
<div class="metrics">{metrics_rows}</div>

{benchmark_section}

<div class="verdict">
<strong>结论:</strong> {report.one_line_verdict or "无"}
</div>

<h2>交易记录 ({report.total_trades} 笔)</h2>
<table>
<tr><th>代码</th><th>方向</th><th>日期</th><th>价格</th><th>数量</th><th>盈亏</th></tr>
{trade_rows}
</table>

<h2>净值曲线</h2>
<div id="equity-chart" style="height:300px;background:#f8f9fa;border-radius:6px;display:flex;align-items:flex-end;padding:10px;">
<span style="color:#888;margin:auto;">数据点: {len(report.equity_curve)}</span>
</div>

</div>
</body>
</html>"""

    @staticmethod
    def _metrics_section(report) -> str:
        ret_cls = "positive" if report.total_return >= 0 else "negative"
        cards = [
            ("总收益", f"{report.total_return:.2%}", ret_cls),
            ("年化收益", f"{report.annual_return:.2%}", ret_cls),
            ("夏普比率", f"{report.sharpe:.2f}", ""),
            ("最大回撤", f"{report.max_drawdown:.2%}", "negative" if report.max_drawdown > 0.1 else ""),
            ("胜率", f"{report.win_rate:.2%}", ""),
            ("交易次数", str(report.total_trades), ""),
            ("基准收益", HTMLReportBuilder._pct(report.benchmark_return, na="基准不可用"), ""),
            ("超额收益", HTMLReportBuilder._pct(report.excess_return, na="基准不可用"), ret_cls),
        ]
        return "\n".join(
            f'<div class="metric-card"><div class="metric-value {cls}">{val}</div>'
            f'<div class="metric-label">{label}</div></div>'
            for label, val, cls in cards
        )

    @staticmethod
    def _trade_section(report) -> str:
        if not report.trade_log:
            return '<tr><td colspan="6" style="text-align:center;color:#888;">无交易记录</td></tr>'
        rows = []
        for t in report.trade_log[-20:]:
            action_label = "买入" if t.get("action") == "buy" else "卖出"
            pnl = t.get("pnl", "")
            pnl_str = f'{pnl:+.2f}' if isinstance(pnl, (int, float)) else ""
            rows.append(
                f'<tr><td>{t.get("code","")}</td><td>{action_label}</td>'
                f'<td>{t.get("date","")}</td><td>{t.get("price",0):.2f}</td>'
                f'<td>{t.get("shares",0)}</td><td>{pnl_str}</td></tr>'
            )
        return "\n".join(rows)

    @staticmethod
    def _benchmark_section(comparison: dict) -> str:
        if not comparison:
            return ""
        # BT-04: 基准不可用 → 显式展示原因, 绝不按 0 假值渲染
        if not comparison.get("available", True):
            reason = comparison.get("reason", "")
            return (
                "\n<h2>基准对比</h2>\n"
                f'<p style="color:#888;">基准不可用: {reason}</p>'
            )
        fmt_pct = HTMLReportBuilder._pct
        fmt_num = HTMLReportBuilder._num2
        excess = comparison.get("excess_return")
        cls = ""
        if isinstance(excess, (int, float)) and not isinstance(excess, bool):
            cls = "positive" if excess >= 0 else "negative"
        return f"""
<h2>基准对比</h2>
<div class="metrics">
<div class="metric-card"><div class="metric-value">{fmt_pct(comparison.get('strategy_return'))}</div><div class="metric-label">策略收益</div></div>
<div class="metric-card"><div class="metric-value">{fmt_pct(comparison.get('benchmark_return'))}</div><div class="metric-label">基准收益 ({comparison.get('index_code', '')})</div></div>
<div class="metric-card"><div class="metric-value {cls}">{fmt_pct(excess)}</div><div class="metric-label">超额收益</div></div>
<div class="metric-card"><div class="metric-value">{fmt_num(comparison.get('information_ratio'))}</div><div class="metric-label">信息比率</div></div>
<div class="metric-card"><div class="metric-value">{fmt_pct(comparison.get('benchmark_max_drawdown'))}</div><div class="metric-label">基准最大回撤</div></div>
<div class="metric-card"><div class="metric-value">{fmt_pct(comparison.get('excess_max_drawdown'))}</div><div class="metric-label">相对最大回撤</div></div>
</div>"""

    @staticmethod
    def save_html(report, filepath: str, benchmark_comparison: dict | None = None) -> str:
        html = HTMLReportBuilder.build(report, benchmark_comparison)
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        return filepath
