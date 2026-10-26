"""db_backup 保留逻辑回归测试。

2026-08-21 23:03 生产事故：原实现 `sorted(glob("qs_*.db*"))` + `while len>3: pop(0)`
一次性删除了 3 份日备份（含当日刚产出那份）、08-17 安全快照、v4 建表锚点，
只因它们同样命中 `qs_*.db*`。本测试锁定修复后的契约。
"""
import os
import time

import pytest


def _touch(path, mtime_offset=0, size=16):
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    if mtime_offset:
        t = time.time() + mtime_offset
        os.utime(path, (t, t))


def _rotate(backup_dir, keep_daily=7):
    """复刻 orchestrator._run_db_backup 的保留段（契约测试）。

    实现变更时本函数须同步；断言表达的是**契约**：
    只轮转 qs_<YYYY-MM-DD>.db.gz，按 mtime 排序，其余文件一概不碰。
    """
    import glob

    dailies = sorted(
        glob.glob(os.path.join(backup_dir, "qs_????-??-??.db.gz")),
        key=os.path.getmtime,
    )
    removed = []
    for old in dailies[:-keep_daily]:
        os.remove(old)
        removed.append(os.path.basename(old))
    return removed


class TestBackupRetentionRegression:
    def test_只轮转日备份_不碰安全备份(self, tmp_path):
        """回归：安全备份/手工备份/staging 残留不得被轮转删除。"""
        d = str(tmp_path)
        # 8 份日备份（mtime 递增）
        for i in range(8):
            _touch(os.path.join(d, f"qs_2026-08-{10+i:02d}.db.gz"), mtime_offset=i)
        # 事故中被误删的那几类文件
        victims = [
            "qs_backup_20260817_2133.db",        # 安全快照
            "qs_pre_go_batch_20260820.db",       # 建表锚点
            "qs_pre_go_batch_20260820.db.gz",
            "qs_backup_20260817_2133.db-shm",    # 只读打开残留
            "qs_backup_20260817_2133.db-wal",
            "qs_2026-08-21.db",                  # staging 中间态
        ]
        for v in victims:
            _touch(os.path.join(d, v))

        removed = _rotate(d, keep_daily=7)

        # 8 份日备份保留 7 份, 只删最旧那 1 份
        assert removed == ["qs_2026-08-10.db.gz"], removed
        # 所有非日备份文件必须原封不动
        for v in victims:
            assert os.path.exists(os.path.join(d, v)), f"{v} 被误删"

    def test_日备份不足保留数时不删任何东西(self, tmp_path):
        d = str(tmp_path)
        for i in range(3):
            _touch(os.path.join(d, f"qs_2026-08-{10+i:02d}.db.gz"), mtime_offset=i)
        assert _rotate(d, keep_daily=7) == []
        assert len(os.listdir(d)) == 3

    def test_按mtime排序而非字典序(self, tmp_path):
        """字典序会把 qs_2026-09-01 排在 qs_2026-10-01 之前——跨年/跨月命名变动时
        字典序与时间序可能背离；契约要求按 mtime。"""
        d = str(tmp_path)
        # 故意让字典序与时间序相反
        _touch(os.path.join(d, "qs_2026-12-31.db.gz"), mtime_offset=-100)  # 最旧
        _touch(os.path.join(d, "qs_2026-01-01.db.gz"), mtime_offset=0)     # 最新
        removed = _rotate(d, keep_daily=1)
        assert removed == ["qs_2026-12-31.db.gz"], removed
        assert os.path.exists(os.path.join(d, "qs_2026-01-01.db.gz"))

    def test_pinned子目录不参与轮转(self, tmp_path):
        d = str(tmp_path)
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        for i in range(9):
            _touch(os.path.join(d, f"qs_2026-08-{10+i:02d}.db.gz"), mtime_offset=i)
        _touch(str(pinned / "qs_pinned_20260821_2306.db.gz"))
        _touch(str(pinned / "qs_2026-08-01.db.gz"), mtime_offset=-999)  # 即使同名模式

        _rotate(d, keep_daily=7)

        assert (pinned / "qs_pinned_20260821_2306.db.gz").exists()
        assert (pinned / "qs_2026-08-01.db.gz").exists(), "pinned/ 内文件不得被轮转"

    @pytest.mark.parametrize("keep", [1, 3, 7])
    def test_保留数生效(self, tmp_path, keep):
        d = str(tmp_path)
        for i in range(10):
            _touch(os.path.join(d, f"qs_2026-08-{10+i:02d}.db.gz"), mtime_offset=i)
        _rotate(d, keep_daily=keep)
        import glob
        assert len(glob.glob(os.path.join(d, "qs_????-??-??.db.gz"))) == keep


class TestBackupChecksumStreaming:
    def test_摘要流式计算不整文件读入内存(self, tmp_path):
        """回归：原实现 hashlib.sha256(f.read()) 会把 2.7GB .gz 一次读入内存。"""
        import hashlib

        p = tmp_path / "blob.gz"
        payload = os.urandom(5 * 1024 * 1024)
        p.write_bytes(payload)

        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)

        assert h.hexdigest() == hashlib.sha256(payload).hexdigest()

    def test_源码不再使用整文件读入(self):
        """守卫：orchestrator 的 db_backup 段不得回退到 f.read() 整读。"""
        import inspect

        from ops_layer import orchestrator

        src = inspect.getsource(orchestrator.PipelineOrchestrator._run_db_backup)
        assert "hashlib.sha256(f.read())" not in src, "回退到整文件读入内存"
        assert "_h.update(_chunk)" in src, "流式摘要缺失"

    def test_源码不再使用宽通配符(self):
        """守卫：不得回退到 glob('qs_*.db*')。"""
        import inspect

        from ops_layer import orchestrator

        src = inspect.getsource(orchestrator.PipelineOrchestrator._run_db_backup)
        # 注释里提到旧模式是允许的, 但 glob( 调用里不许出现
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert 'glob.glob(os.path.join(backup_dir, "qs_*.db*"))' not in stripped, (
                f"回退到宽通配符: {stripped}"
            )
        assert 'qs_????-??-??.db.gz' in src, "限定模式缺失"
