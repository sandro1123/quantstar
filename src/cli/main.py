"""QuantStar CLI — 命令行工具"""
import json
import os
import sys

import click
from agent_layer import build_request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _read_version() -> str:
    """从 pyproject.toml 读取版本(单一事实源), 失败回退 unknown。"""
    try:
        import re
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        with open(os.path.join(root, "pyproject.toml")) as f:
            m = re.search(r'version\s*=\s*"([^"]+)"', f.read())
            return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


__version__ = _read_version()

_app_cache = None


def _lazy_bootstrap():
    global _app_cache
    if _app_cache is None:
        from bootstrap import bootstrap_app
        _app_cache = bootstrap_app()
    return _app_cache


@click.group()
@click.version_option(version=__version__)
@click.option("--json", "json_output", is_flag=True, default=False, help="JSON output mode")
@click.pass_context
def cli(ctx, json_output):
    """QuantStar — A股量化选股系统"""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output


def output(ctx, data: dict):
    """Unified output — human readable or JSON"""
    if ctx.obj.get("json"):
        click.echo(json.dumps(data, ensure_ascii=False))
    else:
        if "message" in data:
            click.echo(data["message"])


@cli.command()
def status():
    """系统状态概览 — 调用 ops 健康检查"""
    try:
        app = _lazy_bootstrap()
        if hasattr(app, "ops") and app.ops:
            report = app.ops.check_health()
            click.echo(f"📊 QuantStar 系统状态 (整体: {report.overall_status})")
            for layer in report.layers:
                icon = "✅" if layer.status == "green" else ("⚠️" if layer.status == "yellow" else "❌")
                click.echo(f"  {icon} {layer.layer}: {layer.summary}")
        else:
            click.echo("  ⚠️ Ops Layer 未初始化")
    except Exception as e:
        click.echo(f"❌ 启动失败: {e}")
        sys.exit(1)


@cli.command()
@click.option("--date", default=None, help="运行日期 (YYYY-MM-DD)")
@click.option("--mode", default="full", type=click.Choice(["full", "strategy", "agent", "report"]))
def run(date, mode):
    """运行完整流水线"""
    from common.utils import bj_date
    date = date or bj_date()
    click.echo(f"🚀 运行流水线: mode={mode}, date={date}")
    try:
        app = _lazy_bootstrap()

        if mode == "full":
            signals = []
            msg = None
            click.echo("  [0/5] 数据管道...")
            if not hasattr(app, "data") or not app.data:
                raise RuntimeError("Data Layer 未初始化")
            if not hasattr(app, "ops") or not app.ops:
                raise RuntimeError("Ops Layer 未初始化，无法运行数据管道")

            existing = 0
            try:
                conn = app.data.db.get_connection()
                existing = conn.execute(
                    "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (date,)
                ).fetchone()[0]
            except Exception as exc:
                click.echo(f"    ⚠️ 无法检查已有数据，将执行管道: {exc}")

            if existing > 0:
                click.echo(f"    ✅ 数据已存在 ({existing} 条 K 线)，跳过管道")
            else:
                pipeline_result = app.ops.run_pipeline("daily_data_pipeline")
                if pipeline_result is None:
                    raise RuntimeError("数据管道未返回运行结果")
                click.echo(f"    数据管道: {pipeline_result.status}")
                # DL-43: partial = 数据已落库但降级, 放行后续步骤但显式提示
                # (与 orchestrator 依赖检查口径一致); 仅 failed/skipped 才中断。
                if pipeline_result.status == "partial":
                    click.echo(
                        f"    ⚠️ 数据管道部分成功: {pipeline_result.error or '无详情'}")
                elif pipeline_result.status != "success":
                    raise RuntimeError(
                        f"数据管道{pipeline_result.status}: "
                        f"{pipeline_result.error or '无错误详情'}"
                    )

            click.echo("  [1/5] 策略运行...")
            if hasattr(app, "strategy") and app.strategy:
                result = app.strategy.run_daily(date)
                signals = result.signals
                click.echo(f"    股票池: {result.stock_count} 只, 信号: {len(signals)} 个")
            else:
                click.echo("    ⚠️ Strategy Layer 未初始化")

            click.echo("  [2/5] Agent 分析...")
            if hasattr(app, "agent") and app.agent and signals:
                for s in signals[:5]:
                    req = build_request(
                        task="stock_analysis",
                        input={"code": s.code, "date": date},
                    )
                    resp = app.agent.analyze(req)
                    click.echo(f"    {s.code}: {resp.verdict} (confidence={resp.confidence:.2f})")
            else:
                click.echo("    ⚠️ Agent Layer 未初始化或无信号")

            click.echo("  [3/5] 报告生成...")
            if hasattr(app, "report") and app.report:
                msg = app.report.build_evening_report(date)
                if msg:
                    click.echo(f"    ✅ 报告: {msg.title}")
            else:
                click.echo("    ⚠️ Report Layer 未初始化")

            click.echo("  [4/5] 消息推送...")
            if hasattr(app, "delivery") and app.delivery and msg:
                app.delivery.send(msg, report_mode="production")
                click.echo("    ✅ 推送完成")
            else:
                click.echo("    ⚠️ Delivery Layer 未初始化或报告为空")

            click.echo("✅ 流水线运行完成")

        elif mode == "strategy":
            if hasattr(app, "strategy") and app.strategy:
                result = app.strategy.run_daily(date)
                signals = result.signals
                click.echo(f"  股票池: {result.stock_count} 只")
                click.echo(f"  市场状态: {result.regime}")
                click.echo(f"  生成信号: {len(signals)} 个")
                for s in signals[:10]:
                    click.echo(f"    {s.action.upper()} {s.code} (强度: {s.strength:.1%}) - {s.reason}")
            else:
                click.echo("  ⚠️ Strategy Layer 未初始化")

        elif mode == "agent":
            if hasattr(app, "agent") and app.agent:
                req = build_request(task="market_analysis", input={"date": date})
                resp = app.agent.analyze(req)
                click.echo(f"  ✅ Agent 分析完成 (verdict={resp.verdict})")
            else:
                click.echo("  ⚠️ Agent Layer 未初始化")

        elif mode == "report":
            if hasattr(app, "report") and app.report:
                msg = app.report.build_evening_report(date)
                if msg:
                    click.echo(f"  ✅ 报告: {msg.title}")
            else:
                click.echo("  ⚠️ Report Layer 未初始化")

    except Exception as e:
        click.echo(f"❌ 运行失败: {e}")
        import traceback
        click.echo(traceback.format_exc())
        sys.exit(1)


@cli.command()
@click.option("--date", default=None)
@click.option("--code", default="000001", help="股票代码")
@click.option("--capital", default=1000000, help="初始资金")
@click.option("--html", is_flag=True, default=False, help="生成 HTML 回测报告")
@click.option("--output", default=None, help="HTML 报告输出路径 (默认 reports/backtest_<code>_<date>.html)")
def backtest(date, code, capital, html, output):
    """运行回测"""
    from common.utils import bj_date
    date = date or bj_date()
    click.echo(f"📈 回测: {code}, 资金={capital:,.0f}, 日期={date}")
    try:
        from backtest_layer.core.models import BacktestInput, CostConfig
        app = _lazy_bootstrap()
        if hasattr(app, "backtest") and app.backtest:
            bt_input = BacktestInput(
                mode="hold",
                codes=[code],
                date_range=(date, date),
                cost=CostConfig(initial_capital=float(capital)),
            )
            result = app.backtest.run(bt_input)
            click.echo(f"  收益率: {result.total_return:.2%}")
            click.echo(f"  最大回撤: {result.max_drawdown:.2%}")
            click.echo(f"  夏普比率: {result.sharpe:.2f}")
            click.echo(f"  交易次数: {result.total_trades}")
            if html:
                # BT-11 ②: HTMLReportBuilder 接线 — 回测结果产出用户可见的 HTML 报告
                from backtest_layer.results.report import HTMLReportBuilder
                out = output or f"reports/backtest_{code}_{date}.html"
                path = HTMLReportBuilder.save_html(result, out)
                click.echo(f"  📄 HTML 报告: {path}")
        else:
            click.echo("  ⚠️ Backtest Layer 未初始化")
    except Exception as e:
        click.echo(f"❌ 回测失败: {e}")
        sys.exit(1)


@cli.command()
@click.option("--type", "report_type", default="evening", type=click.Choice(["morning", "midday", "evening", "weekly"]))
@click.option("--date", default=None)
def report(report_type, date):
    """生成报告"""
    from common.utils import bj_date
    date = date or bj_date()
    click.echo(f"📝 生成报告: {report_type} ({date})")
    try:
        app = _lazy_bootstrap()
        if not hasattr(app, "report") or not app.report:
            click.echo("  ⚠️ Report Layer 未初始化")
            sys.exit(1)
        builder_map = {
            "morning": app.report.build_morning_brief,
            "midday": app.report.build_midday_brief,
            "evening": app.report.build_evening_report,
            "weekly": app.report.build_weekly_summary,
        }
        msg = builder_map[report_type](date)
        if msg:
            click.echo(f"  ✅ 报告生成成功: {msg.title}")
        else:
            click.echo("  ⚠️ 报告生成为空")
            sys.exit(1)
    except Exception as e:
        click.echo(f"❌ 生成失败: {e}")
        sys.exit(1)


@cli.command()
@click.pass_context
def health(ctx):
    """全层健康检查"""
    try:
        app = _lazy_bootstrap()
        if hasattr(app, "ops") and app.ops:
            report = app.ops.check_health()
            msg_lines = ["🏥 健康检查", f"  整体状态: {report.overall_status}"]
            for layer in report.layers:
                msg_lines.append(f"  [{layer.status.upper()}] {layer.layer}: {layer.summary}")
            data = {"command": "health", "status": report.overall_status,
                    "layers": [{"name": layer.layer, "status": layer.status, "summary": layer.summary} for layer in report.layers],
                    "message": "\n".join(msg_lines)}
            output(ctx, data)
            if report.overall_status == "red":
                ctx.exit(1)
        else:
            output(ctx, {"command": "health", "status": "error", "message": "Ops not initialized"})
            ctx.exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as e:
        output(ctx, {"command": "health", "status": "error", "message": str(e)})
        ctx.exit(1)


@cli.command()
def version():
    """版本信息"""
    click.echo(f"QuantStar v{__version__}")
    try:
        import subprocess
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        click.echo(f"  Git: {branch}@{commit}")
    except Exception:
        click.echo("  Git: unavailable")


@cli.group()
def pipeline():
    """管道管理"""


def _get_cli_db():
    from common.database import Database

    return Database()


def _record_cli_failure(name: str, error: str) -> None:
    """F6.2: CLI 崩溃落库 — 收口悬空 running 记录为 failed + 告警 (fail-safe)。

    orchestrator 崩溃时 pipeline_runs 会残留 status='running' 的行;
    若无残留 (start_run 之前崩溃) 则补一条 failed 记录保留痕迹。
    OPS-04: 落库路径包 retry_on_locked (3×2s) — 崩溃落库不再二次撞锁失败。
    """
    try:
        from common.utils import bj_now
        from ops_layer.core.tables import OPS_TABLES_DDL
        from ops_layer.run_guard import retry_on_locked

        def _write():
            db = _get_cli_db()
            conn = db.get_connection()
            conn.executescript(OPS_TABLES_DDL)
            conn.commit()
            rows = conn.execute(
                "SELECT run_id FROM pipeline_runs WHERE pipeline_name=? AND status='running' "
                "ORDER BY start_time DESC",
                (name,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE pipeline_runs SET status='failed', error_msg=?, end_time=? WHERE run_id=?",
                    (error[:2000], bj_now(), r["run_id"]),
                )
            conn.commit()
            if not rows:
                from ops_layer.pipeline_logger import PipelineLogger

                pl = PipelineLogger(db)
                run_id = pl.start_run(name)
                pl.end_run(run_id, "failed", error[:2000])
            from ops_layer.alert_manager import AlertManager
            from ops_layer.core.models import Alert

            AlertManager(db).send(Alert(
                severity="critical", layer="ops",
                title=f"管道 CLI 崩溃: {name}",
                detail=error[:2000],
                action="检查 logs/pipeline.log 与 pipeline_runs",
            ))

        retry_on_locked(_write, label=f"cli_failure:{name}")
    except Exception as e:
        click.echo(f"⚠️ 崩溃落库失败: {e}", err=True)


@pipeline.command("run")
@click.argument("name")
def run_pipeline_cli(name):
    """运行指定管道"""
    try:
        app = _lazy_bootstrap()
        if not hasattr(app, "ops") or not app.ops:
            click.echo("⚠️ Ops Layer 未初始化")
            sys.exit(1)
        # OPS-01: 启动收割 — 同名管道的过期 running 记录 (僵尸) 先收为 failed,
        # 否则"已在运行跳过"保护会被僵尸记录利用, 永久堵死管道 (08-17 事故)。
        try:
            ops = app.ops
            if ops._zombie_reap_enabled():
                reaped = ops.pipeline_logger.reap_expired_runs(
                    pipeline_name=name,
                    timeout_lookup=ops._pipeline_timeout_lookup(),
                    # 判例 22 同源修: CLI 同名收割面同传 full_cfg (DL-65
                    # ceiling 含重试/等待窗), 与 ops_layer 全量收割面一致
                    full_cfg=ops._pipeline_full_cfg(),
                )
                for r in reaped:
                    click.echo(f"  🧟 收割僵尸记录: {r['run_id']} (start={r['start_time']})")
        except Exception as e:
            click.echo(f"  ⚠️ 僵尸收割跳过 (fail-safe): {e}", err=True)
        click.echo(f"🚀 运行管道: {name}")
        result = app.ops.run_pipeline(name)
        if result is None:
            click.echo("❌ 管道运行失败: 返回空结果")
            sys.exit(1)
        status = getattr(result, "status", None) or getattr(result, "overall_status", "failed")
        if status == "success":
            click.echo(f"  ✅ 完成 ({result.elapsed_s:.1f}s)")
        elif status == "partial":
            # DL-43: 部分成功 — 数据已落库但降级, 显式提示, 退出码 0 (非硬失败)
            click.echo(f"  ⚠️ 部分成功 ({result.elapsed_s:.1f}s): {result.error or '无详情'}")
        elif status == "skipped":
            click.echo(f"  ⏭️ 跳过: {result.error}")
        else:
            click.echo(f"  ❌ 失败: {result.error}")
            sys.exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as e:
        # F6.2: CLI 崩溃落库 — 收口悬空 running 记录 + 告警, 保持退出码 1
        import traceback

        tb = traceback.format_exc()
        click.echo(f"❌ 管道 {name} 崩溃: {e}", err=True)
        _record_cli_failure(name, tb)
        sys.exit(1)


@pipeline.command(name="list")
def list_pipelines():
    """列出所有管道"""
    app = _lazy_bootstrap()
    if not hasattr(app, "ops") or not app.ops:
        click.echo("⚠️ Ops Layer 未初始化")
        return
    pipelines = app.ops.list_pipelines()
    if not pipelines:
        click.echo("  暂无管道定义")
        return
    click.echo(f"📋 管道列表 ({len(pipelines)}):")
    for pipeline_info in pipelines:
        schedule = pipeline_info.schedule or "-"
        deps = ", ".join(pipeline_info.depends_on) or "无"
        click.echo(f"  {pipeline_info.name:25s} [{schedule:12s}] 依赖: {deps}")


@pipeline.command("status")
@click.argument("name")
def pipeline_status_cli(name):
    """查看管道最新运行状态"""
    app = _lazy_bootstrap()
    if not hasattr(app, "ops") or not app.ops:
        click.echo("⚠️ Ops Layer 未初始化")
        return
    conn = app.ops.db.get_connection()
    row = conn.execute(
        """SELECT * FROM pipeline_runs WHERE pipeline_name = ?
           ORDER BY start_time DESC LIMIT 5""",
        (name,),
    ).fetchall()
    if not row:
        click.echo(f"  {name}: 暂无运行记录")
        return
    click.echo(f"📊 {name} 最近运行记录:")
    for r in row:
        click.echo(f"  [{r['status']:7s}] {r['start_time']} ({r['end_time'] or '-'})")


@pipeline.command("schedule-check")
def schedule_check_cli():
    """OPS-05: 调度漂移检测 — ops.yaml 定义 vs crontab 触发对账"""
    from ops_layer.schedule_check import format_drift_report, run_schedule_check

    app = _lazy_bootstrap()
    pipelines = None
    if hasattr(app, "ops") and app.ops:
        pipelines = app.ops.orchestrator._pipeline_configs
    ok, result = run_schedule_check(pipelines=pipelines)
    click.echo(format_drift_report(result))
    if result.get("crontab_unavailable"):
        click.echo("  ⚠️ crontab 不可读, 无法对账")
        sys.exit(1)
    if not ok:
        sys.exit(1)


@pipeline.command()
def dag():
    """显示管道依赖图"""
    app = _lazy_bootstrap()
    if not hasattr(app, "ops") or not app.ops:
        click.echo("⚠️ Ops Layer 未初始化")
        return
    pipelines = app.ops.list_pipelines()
    click.echo("📋 管道依赖图:")
    for pipeline_info in sorted(pipelines, key=lambda item: item.schedule):
        name = pipeline_info.name
        deps = pipeline_info.depends_on
        schedule = pipeline_info.schedule or "-"
        if deps:
            click.echo(f"  {name:25s} [{schedule:12s}] ← {', '.join(deps)}")
        else:
            click.echo(f"  {name:25s} [{schedule:12s}] (无依赖)")


# ---- EX-01: HITL 审核 CLI (qs review list/approve/reject) ----
@cli.group()
def review():
    """人工审核队列管理 (HITL 审批闭环)"""


def _get_human_in_loop():
    app = _lazy_bootstrap()
    if not hasattr(app, "execution") or not app.execution:
        click.echo("⚠️ Execution Layer 未初始化")
        return None, None
    pm = app.execution.plan_manager
    return pm, pm.human_in_loop


@review.command("list")
@click.option("--status", default="pending", type=click.Choice(["pending", "approved", "all"]),
              help="筛选状态")
def review_list(status):
    """列出审核队列"""
    pm, h = _get_human_in_loop()
    if not h:
        return
    conn = h.db.get_connection()
    if status == "all":
        rows = conn.execute("SELECT * FROM human_review_queue ORDER BY created_at DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM human_review_queue WHERE status = ? ORDER BY created_at DESC LIMIT 50",
            (status,)).fetchall()
    if not rows:
        click.echo(f"（无 {status} 审核记录）")
        return
    click.echo(f"📋 审核队列 ({status}): {len(rows)} 条")
    for r in rows:
        r = dict(r)
        params = f"entry[{r.get('entry_low')},{r.get('entry_high')}] " \
                 f"sl={r.get('stop_loss')} tp={r.get('take_profit')}"
        click.echo(f"  #{r['id']} [{r['status']}] {r['code']} {r['decision_type']} "
                   f"conf={r.get('confidence')} {params}")
        if r.get("reason"):
            click.echo(f"       理由: {r['reason']}")


@review.command("approve")
@click.argument("review_id", type=int)
@click.option("--note", default="", help="审核备注")
def review_approve(review_id, note):
    """批准审核记录并消费到执行池"""
    pm, h = _get_human_in_loop()
    if not h:
        return
    if h.approve(review_id, reviewer="cli", note=note):
        click.echo(f"✅ 已批准 #{review_id}")
        n = pm.consume_approved()
        click.echo(f"   消费 {n} 条批准记录到执行池")
    else:
        click.echo(f"❌ 批准失败: #{review_id} 不存在或非 pending 状态")


@review.command("reject")
@click.argument("review_id", type=int)
@click.option("--note", default="", help="拒绝理由")
def review_reject(review_id, note):
    """拒绝审核记录"""
    pm, h = _get_human_in_loop()
    if not h:
        return
    if h.reject(review_id, reviewer="cli", note=note):
        click.echo(f"✅ 已拒绝 #{review_id}")
    else:
        click.echo(f"❌ 拒绝失败: #{review_id} 不存在或非 pending 状态")


@review.command("veto")
@click.argument("review_id", type=int)
@click.option("--note", default="", help="否决理由")
def review_veto(review_id, note):
    """否决已自动批准的记录 (Phase 1.6, 双订阅未解期间的可靠否决通道)

    三态联动: 未消费=仅翻转; pending_entry=撤入场; active=模拟仓退出(T+1)。
    """
    pm, h = _get_human_in_loop()
    if not h:
        return
    row = h.get(review_id)
    if not row:
        click.echo(f"❌ 否决失败: #{review_id} 不存在")
        return
    consumed = bool(row.get("consumed"))
    if not h.veto(review_id, reviewer="cli", note=note):
        click.echo(f"❌ 否决失败: #{review_id} 当前状态 {row.get('status')} (仅 approved 可否决)")
        return
    click.echo(f"✅ 已否决 #{review_id}")
    if not consumed:
        click.echo("   未消费, 仅状态翻转 (不会被 consume_approved 消费)")
        return
    code = row.get("code") or ""
    current = pm.stock_pool.get(code)
    status = (current or {}).get("status")
    if status == "pending_entry":
        pm.stock_pool.update_status(code, "watch")
        pm.action_record.write(code=code, action_type="cancel_entry",
                               reason=f"人工否决自动批准 (review_id={review_id})",
                               source="manual")
        click.echo(f"   {code} 已撤入场 (pending_entry → watch)")
    elif status == "active":
        pm.stock_pool.mark_exit_trigger(
            code, (current or {}).get("last_price") or 0,
            f"人工否决自动批准 (review_id={review_id})")
        click.echo(f"   {code} 模拟仓已标记退出 (T+1 结算)")
    else:
        click.echo(f"   {code} 池内状态 {status}, 无联动动作")

# ---- OPS-07: 告警生命周期 CLI (qs alert list/resolve/dismiss) ----
@cli.group()
def alert():
    """告警生命周期管理 (open → resolved/dismissed)"""


def _get_alert_manager():
    app = _lazy_bootstrap()
    if not hasattr(app, "ops") or not app.ops:
        click.echo("⚠️ Ops Layer 未初始化")
        return None
    return app.ops.alert_manager


@alert.command("list")
@click.option("--status", default="open", type=click.Choice(["open", "resolved", "dismissed", "all"]),
              help="筛选状态")
def alert_list(status):
    """列出告警"""
    am = _get_alert_manager()
    if not am:
        return
    conn = am.db.get_connection()
    if status == "all":
        rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE status = ? ORDER BY created_at DESC LIMIT 50",
            (status,)).fetchall()
    if not rows:
        click.echo(f"（无 {status} 告警）")
        return
    click.echo(f"🔔 告警列表 ({status}): {len(rows)} 条")
    for r in rows:
        r = dict(r)
        click.echo(f"  #{r['id']} [{r['severity']:8s}] [{r['status']:9s}] {r['title']}")
        click.echo(f"       {r['created_at']}  layer={r['layer']}")
        if r.get("detail"):
            click.echo(f"       {str(r['detail'])[:120]}")


@alert.command("resolve")
@click.argument("alert_id", type=int)
def alert_resolve(alert_id):
    """标记告警已解决"""
    am = _get_alert_manager()
    if not am:
        return
    if am.resolve(alert_id):
        click.echo(f"✅ 已解决 #{alert_id}")
    else:
        click.echo(f"❌ 解决失败: #{alert_id} 不存在或非 open 状态")


@alert.command("dismiss")
@click.argument("alert_id", type=int)
def alert_dismiss(alert_id):
    """驳回告警 (无需处理/误报)"""
    am = _get_alert_manager()
    if not am:
        return
    if am.dismiss(alert_id):
        click.echo(f"✅ 已驳回 #{alert_id}")
    else:
        click.echo(f"❌ 驳回失败: #{alert_id} 不存在或非 open 状态")


# ── DL-30: 数据目录 (铁律 #13 基建) ──

@cli.group()
def data():
    """数据层工具"""


@data.command("catalog")
@click.option("--date", "audit_date", default=None,
              help="新鲜度审计日期 (默认今天, YYYY-MM-DD)")
@click.pass_context
def data_catalog(ctx, audit_date):
    """数据目录 — 读 API / 表 / 管道, 从代码与注册表自动派生"""
    from common.utils import bj_now
    from data_layer import DataLayer
    from data_layer.catalog import build_catalog

    date = audit_date or bj_now()[:10]
    layer = DataLayer(db=_get_cli_db())
    cat = build_catalog(layer, date)
    if ctx.obj.get("json"):
        click.echo(json.dumps(cat, ensure_ascii=False))
        return
    click.echo(f"📚 数据目录 (审计日期 {date})")
    click.echo(f"\n读 API ({len(cat['read_apis'])}):")
    for e in cat["read_apis"]:
        click.echo(f"  {e['signature']:50s} -> {e['return_type']:20s} {e['description'][:40]}")
    click.echo(f"\n表 ({len(cat['tables'])}):")
    for t in cat["tables"]:
        md = t["max_date"] or "-"
        behind = "-" if t["days_behind"] is None else str(t["days_behind"])
        icon = "✅" if t["status"] == "ok" else ("⏰" if t["status"] == "delayed" else "❌")
        click.echo(f"  {icon} {t['table']:25s} rows={t['row_count']:<10d} "
                   f"max_date={md:12s} behind={behind:4s} [{t['status']}]")
    click.echo(f"\n管道 ({len(cat['pipelines'])}):")
    for pl in cat["pipelines"]:
        click.echo(f"  {pl['name']:25s} produces={','.join(pl['produces']) or '-'}")
    if cat["stale_tables"]:
        click.echo(f"\n⚠️ 停滞表: {', '.join(cat['stale_tables'])}")


# advisory 投顾命令组 (只读, MVP P1) — 独立模块不膨胀 main.py
from cli.advisory import advisory  # noqa: E402
cli.add_command(advisory)

# issues 每日问题报告命令组 (只读, 批 1) — 独立模块不膨胀 main.py
from cli.issues import issues  # noqa: E402
cli.add_command(issues)


# recon 对账命令组 (只读, ddp 两小卡②) — 独立模块不膨胀 main.py
from cli.recon import recon  # noqa: E402
cli.add_command(recon)

if __name__ == "__main__":
    cli()
