"""盘中扫描 — crontab 每 5 分钟执行 (09:30-15:00)"""
from common.logging import get_logger, setup_logging

# setup_logging 必须先于 get_logger 与各层导入 (同 daily_update.py)
setup_logging(profile="prod")
logger = get_logger("execution.script.intraday_scan")

# P0-1: cron 直驱入口不经 src/__main__.py, 需自带 load_dotenv (与 __main__.py:12 同款)。
# CF-01 之后 CONFIG_ENV=prod 缺密钥硬抛 RuntimeError — 2026-08-21 单日崩 55+3 次的根因。
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env"))

from common.database import Database
from config_layer import Config
from data_layer import DataLayer
from execution_layer import ExecutionLayer
from strategy_layer import StrategyLayer


def main():
    db = Database()
    config = Config()
    config.load()
    data_layer = DataLayer(config=config, db=db)
    data_layer._db = db
    strategy_layer = StrategyLayer(config=config, data_layer=data_layer, db=db)
    # AG-WIRE-4 (统筹 2026-09-08): 盘中 agent 腿通电 — 原构造漏传 agent_layer
    # (默认 None → EntryEngine/ExitEngine/IntradayMonitor 首行早退, AG-WIRE-1
    # 顺查定谳: intraday_checks 651行/日在跑但 agent_checkpoints 盘中零行)。
    # 按 bootstrap.py:90-98 先例: AgentLayer 非关键层, try 包裹失败不阻断扫描。
    agent_layer = None
    try:
        from agent_layer import AgentLayer
        agent_layer = AgentLayer(db, config)
        agent_layer.start()
    except Exception as e:  # agent 层缺席 → 扫描仍跑, entry/exit 腿维持原降级
        get_logger("execution.intraday").warning(
            f"AgentLayer 启动失败, 盘中 agent 腿降级: {e}")
    el = ExecutionLayer(db=db, config=config, data_layer=data_layer,
                        strategy_layer=strategy_layer, agent_layer=agent_layer)
    el.start()
    el.intraday_scan()
    # EX-4 CLI 挂线 (统筹 sm-141ed9c 授权, 防御形态三要件):
    # 1) try/except 解耦 — 收盘段 (advisor 池维护+三态留痕) 失败不炸主扫描;
    # 2) 失败 warning 不静默 (铁律 #12), 零写降级;
    # 3) 集成测试 test_w_ex4_cli_wire 防未来被"优化"掉 (三次「建了没通电」教训)。
    try:
        from execution_layer.scripts.ex4_cli_scan import run as _ex4_cli_run
        _ex4_cli_run(db)
    except Exception as e:  # noqa: BLE001 — 收盘段是日终附加面, 不阻断主扫描
        get_logger("execution.intraday").warning(
            f"ex4_cli_scan 收盘段失败 (主扫描不受影响, 零写降级): {e}")
    el.shutdown()


if __name__ == "__main__":
    # F6.1: 哨兵包装 — 崩溃写 pipeline_runs(failed) + 飞书告警
    from ops_layer.run_guard import run_guarded

    run_guarded("intraday_scan", main)
