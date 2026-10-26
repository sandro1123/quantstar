"""CLI 适配层 — late_success 对账命令 (ddp 两小卡②, 只读)。

薄壳: 只做参数解析与 human/JSON 输出切换, 对账逻辑全在
ops_layer.late_success_recon (FOOTPRINTS/find/recon/run_recon/main)。
命令注册: main.py 尾部 `from cli.recon import recon; cli.add_command(recon)`,
与 advisory/issues 同一模式。
"""
import click

from ops_layer.late_success_recon import main as recon_main


def _resolve_db() -> str:
    import os
    p = os.environ.get("QS_TEST_DB", "")
    if p:
        return p
    from config_layer import cfg_get
    root = cfg_get("data.db_path", "data/qs.db")
    return str(root) if str(root).startswith("/") else root


@click.group()
def recon():
    """对账工具 (只读): pipeline_runs 状态 vs 实际落库自检。"""


@recon.command(name="late-success")
@click.option("--date", "date", default="", help="对账日 (默认今天)")
@click.option("--pipeline", "pipeline", default="", help="只核指定管道")
@click.option("--db", "db_path", default="", help="DB 路径 (默认生产 qs.db)")
@click.pass_context
def late_success(ctx, date, pipeline, db_path):
    """补跑链 late_success 状态与实际落库对账。

    exit 0 = 对账通过 (或当日无补跑链); exit 1 = 存在 mismatch/error,
    即有 status=success 但数据未落库 — 宁 NULL 不假值, 不静默吞。
    """
    as_json = bool(ctx.obj and ctx.obj.get("json"))
    rc = recon_main(db_path=db_path or _resolve_db(), date=date,
                    pipeline=pipeline, as_json=as_json)
    ctx.exit(rc)
