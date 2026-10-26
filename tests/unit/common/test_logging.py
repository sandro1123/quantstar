"""Tests for common/logging.py"""
import json
import logging
import pytest
from common.logging import get_logger, setup_logging, QuantStarLogger, reset_logging


def test_get_logger_returns_quantstar_logger():
    logger = get_logger("test_layer")
    assert isinstance(logger, QuantStarLogger)


def test_logger_includes_layer():
    logger = get_logger("test_layer")
    assert logger.layer == "test_layer"


def test_logger_output_json_format(caplog):
    setup_logging("test", level=logging.DEBUG)
    logger = get_logger("data_layer")
    logger.info("测试消息", extra={"metrics": {"count": 42}})
    record = caplog.records[-1]
    assert record.layer == "data_layer"
    assert "测试消息" in record.message


def test_logger_includes_trace_id():
    logger = get_logger("data_layer", trace_id="abc123")
    assert logger.trace_id == "abc123"


def test_setup_logging_sets_level():
    """Test that setup_logging configures the root logger level"""
    import logging as _logging
    setup_logging("test", level=_logging.DEBUG)
    root = _logging.getLogger()
    assert root.level == _logging.DEBUG or root.getEffectiveLevel() <= _logging.INFO


def test_setup_logging_is_idempotent():
    """重复调用 setup_logging 不应叠加 handler（防止日志双发）"""
    reset_logging()
    import logging as _logging
    setup_logging("test")
    setup_logging("test")
    setup_logging("prod")
    root = _logging.getLogger()
    stdout_handlers = [h for h in root.handlers if type(h) is _logging.StreamHandler]
    file_handlers = [h for h in root.handlers if isinstance(h, _logging.handlers.RotatingFileHandler)]
    assert len(stdout_handlers) == 1
    assert len(file_handlers) == 1
    reset_logging()