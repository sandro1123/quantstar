# -*- coding: utf-8 -*-
"""db_backup 假绿 + disk_check 保留块盲区卡 — RED 钉测 (施工前应红).

三病修法对应 (设计稿 md5=538d9401, 统筹裁定 sm-1883f0):
  修1 db_backup 跳过≠成功: free<10 → HandlerResult(success=False) + critical 告警落行
  修2 disk_check df 口径 (used/(used+avail), f_bavail 基) + 双判据 (pct≥85 或 avail<20G)
     阈值落 ops.yaml Config 全键空间 (#108 _cfg_all 先例), 测试用默认值
  修3 AlertManager.send 通道反转: 去重读 DB → 推 Feishu → INSERT fail-safe,
     保 P0-3 去重状态机 (INSERT 失败同进程不重复推)

RED 证据基线 (2026-09-04 10:46 mode=ro 取证, 设备实值):
  statvfs: total=231.2G bfree=23.7G bavail=13.3G 保留块=10.5G (4.52 pt)
  shutil 口径 used/total = 89.73% → 旧算法 `pct > 90` 不触发 (盲区内)
  df 口径 used/(used+avail) = 207.5/220.8 = 93.97% ≥ 85 → 新算法命中
  db_backup 09-02/09-03 23:00 假绿两晚: runs success+0s+error 空, log 有 WARNING 痕,
  data/backups 无对应 .db.gz

runs 行 failed 映射 = _end_run_safe 既有机制 (orchestrator.py:500-502 已核),
handler 级 success=False + errors 非空即产 failed 行, 不在本文件重复测机械层。
"""
import os
import sqlite3
import sys

_QS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_QS_ROOT, "src"))

from ops_layer.alert_manager import AlertManager  # noqa: E402
from ops_layer.core.models import Alert  # noqa: E402
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


def _disk_alert_rows(db):
    conn = db.get_connection()
    return conn.execute(
        "SELECT severity, title FROM alerts WHERE title LIKE '%磁盘%'"
    ).fetchall()


# ── 修1: db_backup 跳过 = failed + 落痕 + critical 告警 ────────────────

def test_db_backup_skip_is_failed_and_loud(monkeypatch):
    """RED: free<10 跳过不得 success — 须 failed+errors 落痕+critical 告警.

    现状 (应红): 守卫 `free_pct < 10 → return HandlerResult(success=True, errors=[])`
    → runs 行假绿 (09-02/09-03 23:00 两晚实证), 零告警。
    """
    import os as _os

    monkeypatch.setattr(
        _os, "statvfs",
        lambda path: _StatvfsStub(total_g=231.2, bfree_g=23.7, bavail_g=18.5),
    )  # avail 18.5G/231.2G = 8.0% free < 10% → 跳过分支

    db, orch = _orchestrator()
    result = orch._run_db_backup({})

    # 跳过不是成功 (铁律#12)
    assert result.success is False, (
        f"db_backup 跳过不得 success=True (假绿) — 实得 success={result.success}"
    )
    assert result.errors and "skipped" in result.errors[0].lower() and "%" in result.errors[0], (
        f"跳过须 errors 落痕 (disk low X% free, backup skipped) — 实得 {result.errors}"
    )
    # 跳过即告警: critical 级行
    rows = _disk_alert_rows(db)
    assert rows and rows[0]["severity"] == "critical", (
        f"跳过须发 critical 磁盘告警 — 实得 {len(rows)} 行: {[dict(r) for r in rows]}"
    )


# ── 修2: disk_check df 口径 + 双判据 ──────────────────────────────────

def test_disk_check_df_caliber_fires_in_reserve_blind_zone(monkeypatch):
    """RED: 保留块盲区 — 今晨真值 shutil 口径 89.73% 漏报, df 口径 93.97% 须命中.

    真值 (09-04 10:46 statvfs probe): total=231.2G bfree=23.7G bavail=13.3G
    旧算法: used/total = 89.73% < 90 → 不告警 (盲区, alerts 全史 0 行实证)
    新算法: df 口径 used/(used+avail) = 93.97% ≥ 85 → critical 告警
    双判据第二腿: avail 13.3G < 20G 也命中。
    """
    import os as _os

    monkeypatch.setattr(
        _os, "statvfs",
        lambda path: _StatvfsStub(total_g=231.2, bfree_g=23.7, bavail_g=13.3),
    )
    db, orch = _orchestrator()
    orch._run_disk_check({})

    rows = _disk_alert_rows(db)
    assert rows and rows[0]["severity"] == "critical", (
        "df 口径 93.97% (shutil 口径仅 89.73% 盲区内) 须发 critical — "
        f"实得 {len(rows)} 行: {[dict(r) for r in rows]}"
    )
    assert rows[0]["title"] == "磁盘空间严重不足"


def test_disk_check_warning_band_uses_warning_severity(monkeypatch):
    """85%≤使用率<92% 且余量充足时只提醒，不得误报 critical。"""
    import os as _os

    monkeypatch.setattr(
        _os, "statvfs",
        lambda path: _StatvfsStub(total_g=231.2, bfree_g=40.0, bavail_g=30.0),
    )  # df: 201.2/(201.2+30)=87.0%; avail 30G≥20G
    db, orch = _orchestrator()
    orch._run_disk_check({})

    rows = _disk_alert_rows(db)
    assert len(rows) == 1
    assert rows[0]["severity"] == "warning"
    assert rows[0]["title"] == "磁盘空间偏高"


def test_disk_check_absolute_floor_small_disk(monkeypatch):
    """RED: 双判据绝对余量腿 — 小盘低比例 (df 口径 60.9%<85) 但余 18G<20G 须命中.

    防只修百分比口径漏掉绝对余量锚 (三口径并存时 GB 是唯一无歧义锚)。
    """
    import os as _os

    monkeypatch.setattr(
        _os, "statvfs",
        lambda path: _StatvfsStub(total_g=50.0, bfree_g=22.0, bavail_g=18.0),
    )  # df: 28/(28+18)=60.9% <85; avail 18G <20G → 仅绝对腿命中
    db, orch = _orchestrator()
    orch._run_disk_check({})

    rows = _disk_alert_rows(db)
    assert rows and rows[0]["severity"] == "critical", (
        f"小盘余量 18G<20G 须走绝对腿发 critical — 实得 {len(rows)} 行"
    )


def test_disk_check_healthy_disk_no_alert(monkeypatch):
    """控制面 (非 RED): 健康盘 (df 69.7%<85 且余 70G≥20G) 不得误报.

    防「无条件告警」假绿实现; 本测现状/修后均应绿, 若修后变红=实现过度触发。
    """
    import os as _os

    monkeypatch.setattr(
        _os, "statvfs",
        lambda path: _StatvfsStub(total_g=231.2, bfree_g=80.0, bavail_g=70.0),
    )
    db, orch = _orchestrator()
    orch._run_disk_check({})

    rows = _disk_alert_rows(db)
    assert not rows, f"健康盘不得告警 — 实得 {[dict(r) for r in rows]}"


# ── 修3: send() 通道反转 — Feishu 先推, INSERT fail-safe, 去重保形 ──────

class _ReadOkWriteDeadConn:
    """ENOSPC 形态连接: SELECT 可用 (去重读), INSERT 抛 OperationalError."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("INSERT"):
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *args)

    def commit(self):
        self._real.commit()


def test_alert_push_survives_insert_failure_no_repeat(monkeypatch):
    """RED: 满盘时 INSERT 炸不得吞 Feishu 推送; 同进程不重复推 (去重保形).

    现状 (应红): send() 先 INSERT 后推 → INSERT 炸则推送走不到 (09-04 02:43
    满盘零飞书实证); 通道反转后去重判定仍读 DB, INSERT 仅 warning。
    """
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()

    am = AlertManager(db)
    real_conn = db.get_connection()
    monkeypatch.setattr(
        db, "get_connection", lambda: _ReadOkWriteDeadConn(real_conn)
    )

    pushes = []
    monkeypatch.setattr(
        type(am), "push_feishu",
        lambda self, title, body, severity="warning":
            pushes.append(title) or True,
    )

    alert = Alert(
        severity="critical", layer="ops", title="磁盘空间不足",
        detail="test", action="test",
    )
    # 第一推: INSERT 炸, 推送必须仍发生
    try:
        sent1 = am.send(alert)
    except sqlite3.OperationalError:
        sent1 = None  # 现状: INSERT 未包 fail-safe → 异常上抛吞推送
    assert sent1 is True and len(pushes) == 1, (
        f"INSERT 炸时 Feishu 须已推 (通道反转) — 实得 sent1={sent1}, pushes={pushes}"
    )
    # 第二推: 同名去重保形 — 不得因 INSERT 失败而重复推
    sent2 = am.send(alert)
    assert sent2 is False and len(pushes) == 1, (
        f"同进程同名不得重复推 (去重状态机保形) — 实得 sent2={sent2}, pushes={pushes}"
    )
