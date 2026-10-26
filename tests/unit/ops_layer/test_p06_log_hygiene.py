"""P0-6 测试: tqdm 噪音抑制 + log_rotation 尺寸轮转修复。

覆盖:
- 非 TTY 环境 setup_logging 设置 TQDM_DISABLE (tqdm 导入时读取, 必须先于 akshare 导入)
- 交互终端 (isatty) 不受影响
- log_rotation: 超尺寸文件轮转 (.1~.N 备份链), 30 天过期清理, 活跃文件不动,
  轮转备份本身跳过 (防递归轮转/误删)
"""
import os
import sys
import time

import pytest

from common import logging as qs_logging
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL


class _FakeStdout:
    def __init__(self, isatty: bool):
        self._isatty = isatty

    def isatty(self):
        return self._isatty


class TestTqdmSuppression:
    def test_non_tty_sets_tqdm_disable(self, monkeypatch):
        monkeypatch.delenv("TQDM_DISABLE", raising=False)
        monkeypatch.setattr(sys, "stdout", _FakeStdout(isatty=False))
        qs_logging._suppress_third_party_progress()
        assert os.environ.get("TQDM_DISABLE") == "1"

    def test_tty_leaves_tqdm_enabled(self, monkeypatch):
        monkeypatch.delenv("TQDM_DISABLE", raising=False)
        monkeypatch.setattr(sys, "stdout", _FakeStdout(isatty=True))
        qs_logging._suppress_third_party_progress()
        assert "TQDM_DISABLE" not in os.environ

    def test_existing_value_preserved(self, monkeypatch):
        monkeypatch.setenv("TQDM_DISABLE", "custom")
        monkeypatch.setattr(sys, "stdout", _FakeStdout(isatty=False))
        qs_logging._suppress_third_party_progress()
        assert os.environ["TQDM_DISABLE"] == "custom"

    def test_setup_logging_applies_suppression(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TQDM_DISABLE", raising=False)
        monkeypatch.setattr(sys, "stdout", _FakeStdout(isatty=False))
        qs_logging.reset_logging()
        try:
            qs_logging.setup_logging(profile="dev", log_file=str(tmp_path / "q.log"))
            assert os.environ.get("TQDM_DISABLE") == "1"
        finally:
            qs_logging.reset_logging()
            monkeypatch.delenv("TQDM_DISABLE", raising=False)


@pytest.fixture
def db():
    database = Database(":memory:")
    database.get_connection().executescript(OPS_TABLES_DDL)
    database.get_connection().commit()
    yield database
    database.close()


@pytest.fixture
def orch(db):
    from ops_layer.alert_manager import AlertManager
    from ops_layer.orchestrator import PipelineOrchestrator
    from ops_layer.pipeline_logger import PipelineLogger

    return PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    """把 orchestrator 的 project_root 指到 tmp_path, logs/ 完全隔离。"""
    import ops_layer.orchestrator as orch_mod

    fake_file = tmp_path / "src" / "ops_layer" / "orchestrator.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.touch()
    monkeypatch.setattr(orch_mod, "__file__", str(fake_file))
    d = tmp_path / "logs"
    d.mkdir()
    return d


def _aged(path, days=31):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class TestLogRotation:
    def test_oversized_file_rotated(self, orch, logs_dir):
        big = logs_dir / "pipeline.log"
        big.write_bytes(b"x" * 200)
        result = orch._run_log_rotation({"max_bytes": 100, "backup_count": 3})
        assert result.success
        assert not big.exists()
        assert (logs_dir / "pipeline.log.1").read_bytes() == b"x" * 200
        assert result.metadata["rotated"] == ["pipeline.log"]

    def test_backup_chain_shifts(self, orch, logs_dir):
        big = logs_dir / "pipeline.log"
        big.write_bytes(b"new" * 50)
        (logs_dir / "pipeline.log.1").write_bytes(b"old1")
        (logs_dir / "pipeline.log.2").write_bytes(b"old2")
        orch._run_log_rotation({"max_bytes": 100, "backup_count": 3})
        assert (logs_dir / "pipeline.log.1").read_bytes() == b"new" * 50
        assert (logs_dir / "pipeline.log.2").read_bytes() == b"old1"
        assert (logs_dir / "pipeline.log.3").read_bytes() == b"old2"

    def test_old_file_removed(self, orch, logs_dir):
        stale = logs_dir / "ancient.log"
        stale.write_text("old")
        _aged(stale, days=31)
        result = orch._run_log_rotation({"max_bytes": 10 * 1024 * 1024})
        assert result.success
        assert not stale.exists()
        assert result.metadata["removed"] == ["ancient.log"]

    def test_active_small_file_untouched(self, orch, logs_dir):
        active = logs_dir / "intraday.log"
        active.write_text("fresh")
        orch._run_log_rotation({})
        assert active.read_text() == "fresh"

    def test_rotated_backups_skipped(self, orch, logs_dir):
        # 备份文件 (.1) 即使超尺寸/过期也不参与轮转与清理 (防递归)
        backup = logs_dir / "pipeline.log.1"
        backup.write_bytes(b"y" * 200)
        _aged(backup, days=40)
        result = orch._run_log_rotation({"max_bytes": 100})
        assert backup.exists()
        assert result.metadata["rotated"] == []
        assert result.metadata["removed"] == []

    def test_missing_logs_dir_ok(self, orch, tmp_path, monkeypatch):
        import ops_layer.orchestrator as orch_mod

        fake_file = tmp_path / "nowhere" / "orchestrator.py"
        monkeypatch.setattr(orch_mod, "__file__", str(fake_file))
        result = orch._run_log_rotation({})
        assert result.success
