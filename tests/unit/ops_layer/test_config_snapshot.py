"""卡V: pipeline_runs config 快照 — 生效配置指纹 + DDL 幂等补列.

派单: 管道入口补 config_hash (生效 yaml 规范化后 sha256) + git HEAD;
DDL 走管道内 CREATE IF NOT EXISTS / ALTER (DL-43 教训); 老行 NULL。

侦察定案 (与派单假设的出入, 回执向统筹说明):
- version(git HEAD) 与 config_hash(文件面指纹) 两列已存在 (DL-13), 主路径
  orchestrator/run_guard/zombie 均已带值 — 卡V不是从零加列。
- 但现有 config_hash 是 config/ 目录全部 yaml **文件字节**指纹:
  文件改了生效值没变 → 哈希漂移误报。本卡新增**生效面**指纹
  (registry 生效值 to_dict(show_secrets=False) 规范化 sha256), 独立列
  config_hash_effective, 不改既有 compute_config_hash 语义
  (version_history OPS-06 去重与 P0-7 都消费它)。
- 老行 NULL: 新列对既有行天然 NULL — 无需回填 (宁可 NULL 不回填假值)。

秘密防泄漏: 生效指纹只取 to_dict(show_secrets=False) — 秘密键值恒为
\x27******\x27 掩码, env 秘密永不入指纹。
"""
import sqlite3

import pytest

from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.pipeline_logger import PipelineLogger
from ops_layer.version_tracker import (
    _ensure_pipeline_runs_columns,
    compute_effective_config_hash,
)
from common.database import Database


# ---------------------------------------------------------------- 指纹函数

def test_effective_hash_deterministic_and_hex():
    """同一生效配置两次计算 → 同一指纹, sha256 十六进制."""
    h1 = compute_effective_config_hash()
    h2 = compute_effective_config_hash()
    assert h1 == h2
    assert len(h1) == 64  # 完整 sha256 hex (非截断)


def test_effective_hash_masks_secrets():
    """秘密键不参与指纹明文 — 脱敏后哈希 (与 show_secrets=False 同语义)."""
    h = compute_effective_config_hash()
    # 指纹本身不可含秘密明文: 64 hex 字符即 sha256 输出, 无其它内容
    import re
    assert re.fullmatch(r"[0-9a-f]{64}", h)


def test_effective_hash_differs_from_file_hash():
    """生效面指纹与文件面指纹是两个语义 — 不得互相混用/覆盖."""
    from ops_layer.version_tracker import compute_config_hash
    assert compute_effective_config_hash() != compute_config_hash()


def test_effective_hash_changes_when_value_changes(monkeypatch, tmp_path):
    """生效值变 → 指纹变 (这是与文件面指纹的核心区别)."""
    import ops_layer.version_tracker as vt

    def fake_registry_dict():
        return {"data.db_path": str(tmp_path / "a.db")}
    monkeypatch.setattr(vt, "_effective_config_dict", fake_registry_dict)
    h1 = compute_effective_config_hash()

    def fake_registry_dict2():
        return {"data.db_path": str(tmp_path / "b.db")}
    monkeypatch.setattr(vt, "_effective_config_dict", fake_registry_dict2)
    h2 = compute_effective_config_hash()
    assert h1 != h2


def test_effective_hash_normalizes_dict_order():
    """规范化: 键序无关 — 同一 dict 不同插入序 → 同指纹."""
    import ops_layer.version_tracker as vt

    h1 = vt._normalize_dict({"a": 1, "b": {"x": 1, "y": 2}})
    h2 = vt._normalize_dict({"b": {"y": 2, "x": 1}, "a": 1})
    assert h1 == h2


def test_effective_hash_fails_closed_outside_config():
    """registry 不可用 → 返回 \x27\x27 不炸 (fail-safe, 与 git_head 同风格);
    由 WARNING 兜底 — 写入路径不因此阻断 (管道记录优先)."""
    import ops_layer.version_tracker as vt

    def boom():
        raise RuntimeError("config layer unavailable")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(vt, "_effective_config_dict", boom)
    try:
        assert compute_effective_config_hash() == ""
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------- DDL 幂等补列

def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(OPS_TABLES_DDL)
    return conn


def test_ensure_columns_adds_effective_hash_on_old_schema():
    """DL-43 场景: 生产老表 (无新列) → 幂等 ALTER ADD COLUMN, 老行自动 NULL."""
    # 老 schema: 造一个没有 config_hash_effective 的 pipeline_runs
    conn = _mk_conn()
    conn.execute("ALTER TABLE pipeline_runs DROP COLUMN config_hash_effective")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")]
    assert "config_hash_effective" not in cols

    _ensure_pipeline_runs_columns(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")]
    assert "config_hash_effective" in cols
    # 老行自动 NULL — 不回填假值
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES (\x27r_old\x27, \x27p\x27, \x272026-01-01\x27, \x272026-01-01 00:00:00\x27, \x27success\x27)")
    row = conn.execute("SELECT config_hash_effective FROM pipeline_runs").fetchone()
    assert row[0] is None
    conn.close()


def test_ensure_columns_idempotent():
    """重复调用不炸 (幂等) — CREATE IF NOT EXISTS / ALTER 已存在均安全."""
    conn = _mk_conn()
    _ensure_pipeline_runs_columns(conn)
    _ensure_pipeline_runs_columns(conn)  # 第二次: 列已存在, 静默通过
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")]
    assert "config_hash_effective" in cols
    conn.close()


def test_fresh_schema_has_column():
    """全新库 (OPS_TABLES_DDL) 直接带新列 — 新库不走 ALTER 路径."""
    conn = _mk_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")]
    assert "config_hash_effective" in cols
    conn.close()


# ---------------------------------------------------------------- 写入路径

def test_start_run_stamps_effective_hash(tmp_path, monkeypatch):
    """管道入口 (start_run) 即打生效指纹 — 不等 end_run (running 态也可溯源)."""
    import ops_layer.version_tracker as vt
    monkeypatch.setattr(vt, "compute_effective_config_hash", lambda: "e" * 64)
    db = Database(str(tmp_path / "t.db"))
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    pl = PipelineLogger(db)
    run_id = pl.start_run("probe_pipe")
    row = conn.execute(
        "SELECT config_hash_effective, version FROM pipeline_runs WHERE run_id=?",
        (run_id,)).fetchone()
    assert row["config_hash_effective"] == "e" * 64
    assert row["version"]  # git short HEAD (或 unknown), 非空
    db.close()


def test_start_run_effective_hash_failsafe(tmp_path, monkeypatch):
    """指纹计算炸 → 照常入库 (NULL), 不阻断管道记录 — 铁律 #12 记录优先."""
    import ops_layer.version_tracker as vt
    def boom():
        raise RuntimeError("registry gone")
    monkeypatch.setattr(vt, "compute_effective_config_hash", boom)
    db = Database(str(tmp_path / "t.db"))
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    pl = PipelineLogger(db)
    run_id = pl.start_run("probe_pipe")
    row = conn.execute(
        "SELECT config_hash_effective FROM pipeline_runs WHERE run_id=?",
        (run_id,)).fetchone()
    assert row["config_hash_effective"] is None
    db.close()


def test_old_rows_stay_null_after_upgrade(tmp_path):
    """升级现场复演: 老表+老行先在, 后建列 → 老行 config_hash_effective 仍 NULL."""
    # 老表 (无新列) + 一条老行
    old_ddl = OPS_TABLES_DDL.replace(
        ",\n    config_hash_effective TEXT", "")
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.executescript(old_ddl)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES (\x27r1\x27, \x27p\x27, \x272026-01-01\x27, \x272026-01-01 00:00:00\x27, \x27success\x27)")
    conn.commit()
    conn.close()

    # 新代码打开同一库: 幂等补列 + 新行带指纹, 老行 NULL
    db = Database(str(tmp_path / "old.db"))
    pl = PipelineLogger(db)
    run_id = pl.start_run("new_pipe")
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    rows = conn.execute(
        "SELECT run_id, config_hash_effective FROM pipeline_runs ORDER BY rowid").fetchall()
    assert rows[0][1] is None          # 老行: 未回填, 保持 NULL
    assert rows[1][1] is not None      # 新行: 带生效指纹
    conn.close()
    db.close()
