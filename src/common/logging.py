"""统一日志格式 — JSON 格式, 含 trace_id"""

import logging
import logging.handlers
import os
import sys


class QuantStarLogger(logging.Logger):
    def __init__(self, name: str, level: int = logging.NOTSET, trace_id: str | None = None):
        super().__init__(name, level)
        self.layer = name
        self.trace_id = trace_id or ""

    def _log(self, level, msg, args, exc_info=None, extra=None, **kwargs):
        extra = extra or {}
        extra.setdefault("layer", self.layer)
        extra.setdefault("trace_id", self.trace_id)
        super()._log(level, msg, args, exc_info, extra, **kwargs)


class QuantStarFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.000+08:00"),
            "level": record.levelname,
            "layer": getattr(record, "layer", "root"),
            "trace_id": getattr(record, "trace_id", ""),
            "message": record.getMessage(),
        }
        metrics = getattr(record, "metrics", None)
        if metrics:
            log_entry["metrics"] = metrics
        if record.exc_info:
            log_entry["error"] = {
                "type": record.exc_info[0].__name__,
                "detail": str(record.exc_info[1]),
                # 完整 traceback — 此前只存 type/detail, 栈被丢弃,
                # 2026-08-26 closed-database 事故只能靠读码反推故障点。
                "traceback": self.formatException(record.exc_info),
            }
        import json
        return json.dumps(log_entry, ensure_ascii=False)


def get_logger(name: str, trace_id: str | None = None) -> QuantStarLogger:
    logger = logging.getLogger(name)
    if not isinstance(logger, QuantStarLogger):
        logger.__class__ = QuantStarLogger
    logger.layer = name
    logger.trace_id = trace_id or ""
    # #121 (2026-09-05): NOTSET 钉死已拆 — 钉级使 import 期 logger 永久冻结
    # (root=WARNING 时代模块级 get_logger 全钉 WARNING, 后续 setup→INFO 不跟随,
    # #80 ops.orchestrator INFO 149→0 即此形态; 群钉实测 158 个)。NOTSET 走
    # stdlib 原生继承, getEffectiveLevel() 沿祖先链实时查 root。
    return logger


_configured = False


def _suppress_third_party_progress() -> None:
    """P0-6: 非 TTY 环境抑制第三方进度条 (tqdm)。

    血案: cron 把 stdout/stderr 重定向进 logs/pipeline.log, akshare 的 tqdm 进度条
    写入 18850 行 it/s 噪音 (12.4MB 里九成是进度条)。tqdm 在模块导入时读取
    TQDM_DISABLE, 故必须在 tqdm 首次导入前设置 (akshare/pandas 惰性加载,
    setup_logging 先于各层导入运行, 是统一挂点)。交互终端 (isatty) 不受影响。
    """
    if not sys.stdout.isatty() and not os.environ.get("TQDM_DISABLE"):
        os.environ["TQDM_DISABLE"] = "1"


def setup_logging(profile: str = "dev", level: int = logging.INFO, log_file: str | None = None) -> None:
    """幂等初始化日志 — 防止重复调用时叠加 handler 导致日志双发。

    仅首次调用时挂载 handler；后续调用只更新级别，避免重复输出。
    """
    _suppress_third_party_progress()
    global _configured
    logging.setLoggerClass(QuantStarLogger)
    root = logging.getLogger()

    if not _configured:
        formatter = QuantStarFormatter()
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

        if log_file is None:
            # DL-64: 测试隔离 — QS_TEST_LOG_FILE 由 tests/conftest.py 在会话启动时
            # 指向临时目录, 防 pytest 窗口把测试日志写进生产 logs/quantstar.log
            # (与 QS_TEST_DB 同款模式; 生产环境从不设置此变量)。
            log_file = os.environ.get("QS_TEST_LOG_FILE")
        if log_file is None:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            log_file = os.path.join(project_root, "logs", "quantstar.log")
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:
            pass

        _configured = True

    root.setLevel(level)


def reset_logging():
    """测试辅助 — 重置幂等标志并移除全部 handler"""
    global _configured
    _configured = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
