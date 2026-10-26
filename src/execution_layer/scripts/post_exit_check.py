"""退出后跟踪 — crontab 每日 16:00 执行"""
from common.logging import get_logger
from execution_layer import ExecutionLayer

logger = get_logger("execution.script.post_exit_check")


def main():
    el = ExecutionLayer()
    el.start()
    count = el.post_exit_tracker.check()
    logger.info(f"Post-exit check completed: {count} records checked")
    el.shutdown()


if __name__ == "__main__":
    main()
