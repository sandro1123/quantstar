# -*- coding: utf-8 -*-
"""机制卡「db_backup 备份机制最小化」— RED 钉测 (施工前应红).

蓝本: 设计稿 f631becd + RED 备稿 3e31f459, 终裁 sm-59a0321895e3461b 全 GO。
四键: ①双轨 (案2 VACUUM INTO 主 + 案1 config 降级回退) ②pre_* 案甲移 pinned/
(运维动作非代码, manifest 面不在本文件) ③duration 入 run 行由 end_run 既有
机制覆盖 + VACUUM/gzip 分段计时 logger.info 两行 ④守卫式 = df 余量 > 源库×1.2
(getsize 运行时, 不写死; f_bavail 口径与 disk_check df 同基)。

现状 (应红, 8815543 上的 _run_db_backup = 修卡 526be47 后形态):
  无 ×1.2 守卫式 / 无 VACUUM 引擎 / 无 config 引擎键 / 无降级回退 /
  无 tmp 清扫自愈 / 无分段计时 INFO 行。

测试面口径 (踩过的坑, 勿再踩):
  - backup_dir 是相对路径 "data/backups" → 必须 monkeypatch.chdir(tmp_path),
    否则产物落 worktree 真目录 (污染 + 断言错位);
  - 引擎键走 _cfg_all("ops.db_backup_engine", ...) 全键空间读法 (#108 先例),
    cfg 兜底须传嵌套 {"ops": {...}} (平键 "ops.db_backup_engine" 会 MISS);
  - VACUUM ro 连接形态 = sqlite3.connect("file:...?mode=ro", uri=True),
    桩区分引擎连接与 staging 连接靠 uri/mode=ro 标记。
"""
import gzip
import os
import shutil
import sqlite3
import sys

import pytest

_QS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_QS_ROOT, "src"))

from ops_layer.alert_manager import AlertManager  # noqa: E402
from ops_layer.orchestrator import PipelineOrchestrator  # noqa: E402
from ops_layer.pipeline_logger import PipelineLogger  # noqa: E402
from common.database import Database  # noqa: E402

_GIB = 2 ** 30


class _StatvfsStub:
    """按 GiB 构造 statvfs 返回值 (f_frsize=4096 换算整数块)."""

    def __init__(self, total_g, bfree_g, bavail_g):
        self.f_frsize = 4096
        blocks = self.f_frsize
        self.f_blocks = int(total_g * _GIB) // blocks
        self.f_bfree = int(bfree_g * _GIB) // blocks
        self.f_bavail = int(bavail_g * _GIB) // blocks


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _make_real_db(tmp_path, size_mb=2):
    """沙箱小库: 真实 SQLite 文件 + 页填充, VACUUM INTO 真跑对象."""
    path = str(tmp_path / "sandbox.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany(
        "INSERT INTO t (v) VALUES (?)",
        [("x" * 200,) for _ in range(size_mb * 4000)],
    )
    con.commit()
    con.close()
    return path


def _bdir():
    """backup_dir 相对路径口径 — 与生产 _run_db_backup 完全一致."""
    return os.path.join("data", "backups")


def _today():
    from common.utils import bj_now

    return bj_now().split(" ")[0]


def _cfg(engine=None):
    """_cfg_all 全键空间读法的 cfg 兜底形态: 嵌套 dict (平键会 MISS)."""
    if engine is None:
        return {}
    return {"ops": {"db_backup_engine": engine}}


def _alert_rows(db):
    conn = db.get_connection()
    return conn.execute(
        "SELECT severity, title FROM alerts WHERE title LIKE '%备份%'"
    ).fetchall()


def _run(orch, cfg, monkeypatch, tmp_path, db_file, statvfs=None):
    """通用桩: statvfs 健康盘 + chdir tmp_path (相对 backup_dir 落桩域) + db 路径.

    chdir 使 "data/backups" 落 tmp_path/data/backups — 轮换/清扫/gz 全在
    桩域, 零触生产 data/backups 与 worktree 真目录。
    """
    st = statvfs or _StatvfsStub(total_g=231.2, bfree_g=92.0, bavail_g=92.0)
    monkeypatch.setattr(os, "statvfs", lambda path: st)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orch.db, "db_path", db_file)
    return orch._run_db_backup(cfg)


# ── RED-1: ×1.2 守卫式 ───────────────────────────────────────────────

def test_w_mech_guard_source_x12_failed_and_loud(monkeypatch, tmp_path):
    """守卫式: df 余量 < 源库×1.2 → failed + critical 告警, 不产 .gz.

    钉死「新闸 ≠ 修1 旧闸」: 桩 avail=92G/231.2G → free_pct=39.8% ≫ 10%
    (修1 闸不触发), 源库桩 80G → ×1.2 = 96G > 92G (新闸触发)。
    现状 (应红): 无 ×1.2 闸 → 走 staging 路径 success=True 假绿。
    """
    db_file = _make_real_db(tmp_path, size_mb=1)
    db, orch = _orchestrator()

    real_getsize = os.path.getsize

    def _stub_getsize(p):
        if p == db_file:
            return int(80 * _GIB)  # 源库桩 80G → ×1.2 = 96G > avail 92G
        return real_getsize(p)

    monkeypatch.setattr(os.path, "getsize", _stub_getsize)
    result = _run(orch, {}, monkeypatch, tmp_path, db_file)

    assert result.success is False, (
        f"×1.2 守卫不过闸须 failed — 实得 success={result.success} "
        f"(现状无 ×1.2 闸, 修1 闸 free_pct=39.8% 不触发 → 假绿走 staging 路径)"
    )
    assert result.errors and "1.2" in result.errors[0], (
        f"守卫 errors 须含 ×1.2 语义落痕 — 实得 {result.errors}"
    )
    rows = _alert_rows(db)
    assert rows and rows[0]["severity"] == "critical", (
        f"守卫不过闸须发 critical 备份告警 — 实得 {len(rows)} 行"
    )
    # 无产物: 守卫在最前, 不得碰 VACUUM/staging
    assert not os.path.exists(os.path.join(_bdir(), f"qs_{_today()}.db.gz"))


# ── RED-2: VACUUM INTO 引擎 + 病A 消灭 ───────────────────────────────

def test_w_mech_vacuum_engine_no_staging_double(monkeypatch, tmp_path):
    """引擎: VACUUM INTO tmp → gz; tmp 清除 + engine 面可观测.

    翻绿条件: 默认 engine=vacuum_into, 产物链 VACUUM INTO qs_<date>.db.vacuum.tmp
    → gzip → remove tmp, metadata.engine="vacuum_into"。
    现状 (应红): staging 法跑通 (gz 会产), 但 metadata 空 → engine 断言钉死差异;
    staging 裸 .db 中途存在但终态清除 — 病A 钉面 = 全程无 .vacuum.tmp 语义
    由 engine 断言+tmp 清除断言共钉。
    """
    db_file = _make_real_db(tmp_path, size_mb=2)
    db, orch = _orchestrator()
    result = _run(orch, _cfg("vacuum_into"), monkeypatch, tmp_path, db_file)

    assert result.success is True, (
        f"VACUUM 引擎健康盘沙箱库须 success — 实得 {result.success} errors={result.errors}"
    )
    gz = os.path.join(_bdir(), f"qs_{_today()}.db.gz")
    tmp = os.path.join(_bdir(), f"qs_{_today()}.db.vacuum.tmp")
    assert os.path.exists(gz) and os.path.getsize(gz) > 0, (
        f"须产 .gz — {_bdir()} 实得 {sorted(os.listdir(_bdir())) if os.path.isdir(_bdir()) else '无目录'}"
    )
    assert not os.path.exists(tmp), (
        f"VACUUM tmp 须运行后清除 — 实得 {sorted(os.listdir(_bdir()))}"
    )
    assert result.metadata.get("engine") == "vacuum_into", (
        f"metadata.engine 须落 'vacuum_into' (选择面可观测) — 实得 {result.metadata}"
    )


# ── RED-3: 引擎病降级回退 (partial 语义) ──────────────────────────────

def test_w_mech_engine_error_falls_back_to_staging(tmp_path, monkeypatch):
    """降级族: VACUUM 引擎病 → 案1 成功 + partial 落痕 + engine 面可观测.

    翻绿条件: engine=vacuum_into + VACUUM 段抛 OperationalError → except
    落 _backup_via_staging, HandlerResult metadata={"partial": True,
    "engine": "staging"}, errors 落痕 "降级 staging"。
    现状 (应红): 无降级语义 — 现行唯一路径就是 staging (连接放行则 gz 产),
    但 metadata 空 → partial 断言钉死「降级是一等公民语义非隐式顺路」。
    """
    db_file = _make_real_db(tmp_path, size_mb=1)
    db, orch = _orchestrator()

    real_connect = sqlite3.connect

    def _connect(target=None, *a, uri=False, **kw):
        if uri or "mode=ro" in str(target):  # VACUUM 引擎连接 = 引擎病桩
            raise sqlite3.OperationalError("stub engine failure")
        return real_connect(target, *a, uri=uri, **kw)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    result = _run(orch, _cfg("vacuum_into"), monkeypatch, tmp_path, db_file)
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert result.success is True, (
        f"引擎病 (OperationalError) 须降级案1 成功 — 实得 success={result.success} "
        f"errors={result.errors}"
    )
    assert result.metadata.get("partial") is True, (
        f"降级须 partial 语义 (件在但引擎非默认) — 实得 {result.metadata}"
    )
    assert result.metadata.get("engine") == "staging", (
        f"降级 metadata.engine 须落 'staging' — 实得 {result.metadata}"
    )
    assert result.errors and "staging" in "".join(result.errors).lower(), (
        f"降级 errors 须落痕 — 实得 {result.errors}"
    )
    gz = os.path.join(_bdir(), f"qs_{_today()}.db.gz")
    assert os.path.exists(gz), "降级后产物 .gz 须在 (件在)"


# ── RED-4: 盘满族不降级 (清 tmp + failed) ─────────────────────────────

def test_w_mech_enospc_midway_no_fallback_clean_tmp(tmp_path, monkeypatch):
    """盘满族: gzip 段 ENOSPC → 清 tmp + failed, 不落案1, 异常不得逃逸.

    翻绿条件: gzip 阶段 OSError(28) → except → 清 tmp +
    HandlerResult(success=False) (不走 _backup_via_staging — 案1 峰值
    19.1G > 案2 17.7G, 降级空跑再炸)。
    现状 (应红): 无 gzip try/except — ENOSPC 原样逃逸出 _run_db_backup
    (本测捕获后以 escaped 断言钉死「IO 病必须被接住」)。
    """
    db_file = _make_real_db(tmp_path, size_mb=1)
    db, orch = _orchestrator()

    def _enospc(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gzip, "open", _enospc)

    escaped = {"v": None}

    def _call():
        try:
            return orch._run_db_backup(_cfg("vacuum_into"))
        except OSError as e:  # 现状: ENOSPC 无人接住原样逃逸
            escaped["v"] = e
            return None

    result = _run(orch, _cfg("vacuum_into"), monkeypatch, tmp_path, db_file) if False else _call()

    assert escaped["v"] is None, (
        f"ENOSPC 必须被 _run_db_backup 接住 (不得逃逸) — 实得逃逸 {escaped['v']!r}"
    )
    assert result is not None and result.success is False, (
        f"盘满族须 failed (降级腿不救盘满) — 实得 {result}"
    )
    assert not (result and result.metadata.get("partial")), (
        f"盘满族不得 partial (未走降级) — 实得 {result.metadata if result else '?'}"
    )
    leftover = [f for f in os.listdir(_bdir()) if f.endswith(".vacuum.tmp")] if os.path.isdir(_bdir()) else []
    assert leftover == [], (
        f"失败须清 VACUUM tmp 不留死件 — 实得 {leftover}"
    )


# ── RED-5: tmp 清扫自愈 ───────────────────────────────────────────────

def test_w_mech_stale_tmp_swept_on_next_run(tmp_path, monkeypatch):
    """自愈: 开头残留 qs_*.vacuum.tmp → 本轮运行后被清 (不留 14G 死件).

    翻绿条件: _run_db_backup 开头 glob("qs_*.vacuum.tmp") 清扫。
    现状 (应红): 无清扫 — 残留 tmp 原样躺 (中断 VACUUM 的 14G 死件无人管)。
    """
    db_file = _make_real_db(tmp_path, size_mb=1)
    # stale 须落 chdir 后的真 backup_dir (tmp_path/data/backups) —
    # 建错域 (worktree CWD) 时断言域空集 = 假绿 (RED 阶段已踩, 探针钉死)
    monkeypatch.chdir(tmp_path)
    os.makedirs(_bdir(), exist_ok=True)
    stale = os.path.join(_bdir(), "qs_2026-09-06.db.vacuum.tmp")
    with open(stale, "wb") as f:
        f.write(b"stale interrupted vacuum artifact")

    db, orch = _orchestrator()
    result = _run(orch, _cfg("vacuum_into"), monkeypatch, tmp_path, db_file)

    assert result.success is True, f"健康轮须 success — 实得 {result.success} {result.errors}"
    assert not os.path.exists(stale), (
        f"开头须清扫残留 .vacuum.tmp (自愈) — 实得仍存在; 目录: {sorted(os.listdir(_bdir()))}"
    )


# ── RED-6: 案1 显式 config 选择 (退路在 config 不在代码) ─────────────

def test_w_mech_staging_engine_explicit_config(tmp_path, monkeypatch):
    """显式 engine=staging → 案1 路径, .gz 产, 不开 VACUUM ro 连接.

    翻绿条件: ops.db_backup_engine=staging → 直接走 _backup_via_staging,
    metadata.engine="staging"。
    现状 (应红): 无键无分支 — 现行唯一路径就是 staging (gz 产), 但
    metadata 空 → engine 断言钉死「显式选择是一等公民语义」。
    """
    db_file = _make_real_db(tmp_path, size_mb=1)
    db, orch = _orchestrator()

    saw_ro = {"n": 0}
    real_connect = sqlite3.connect

    def _connect(target=None, *a, uri=False, **kw):
        if uri or "mode=ro" in str(target):
            saw_ro["n"] += 1
        return real_connect(target, *a, uri=uri, **kw)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    result = _run(orch, _cfg("staging"), monkeypatch, tmp_path, db_file)
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert result.success is True, f"显式 staging 须 success — {result.success} {result.errors}"
    assert saw_ro["n"] == 0, (
        f"engine=staging 不得开 VACUUM ro 连接 — 实得 {saw_ro['n']} 次"
    )
    gz = os.path.join(_bdir(), f"qs_{_today()}.db.gz")
    assert os.path.exists(gz), "staging 引擎产物 .gz 须在"
    assert result.metadata.get("engine") == "staging", (
        f"metadata.engine 须落 'staging' (选择面可观测) — 实得 {result.metadata}"
    )
