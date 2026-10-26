"""盘后更新 — crontab 每日 16:16 执行 (2026-09-25 修文档漂移: 原写 15:30)。

遇 qs_db_write.lock 被占时由 flock_guard 等待至多 wait_timeout_s=7200s
(ops.pipelines.daily_update), 不再 skip-and-drop。
"""
from common.logging import get_logger, setup_logging

# setup_logging 必须先于 get_logger: 否则 logger 级别被钉在 root 默认 WARNING,
# INFO 级完成日志 ("Daily update completed") 会被静默丢弃
setup_logging(profile="prod")
logger = get_logger("execution.script.daily_update")

# P0-1: cron 直驱入口不经 src/__main__.py, 需自带 load_dotenv (与 __main__.py:12 同款)。
# CF-01 之后 CONFIG_ENV=prod 缺密钥硬抛 RuntimeError — 2026-08-21 单日崩 55+3 次的根因。
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env"))

from common.database import Database
from config_layer import Config
from data_layer import DataLayer
from execution_layer import ExecutionLayer


def main():
    db = Database()
    config = Config()
    config.load()
    data_layer = DataLayer(config=config, db=db)
    data_layer._db = db
    el = ExecutionLayer(db=db, config=config, data_layer=data_layer)
    el.start()
    el.daily_update()
    logger.info("Daily update completed")
    el.shutdown()


if __name__ == "__main__":
    # F6.1: 哨兵包装 — 崩溃写 pipeline_runs(failed) + 飞书告警
    # (替代原 try/except: 异常重新抛出, 退出码仍为 1)
    from ops_layer.run_guard import run_guarded

    run_guarded("daily_update", main)
