# -*- coding: utf-8 -*-
"""增量备份管道 — TDD RED 先行 (统筹亲接管 Codex 未上线件, 2026-09-14)。

v1.0 判据 6/7 (备份异地化+恢复演练) 的测试面。

三族:
A. 核心模块面 — run_incremental_backup / verify_increment (临时库对拍+幂等+空表)
B. 接线三重证 — dispatch 注册 + ops.yaml 条目 + schedule_check 对账面
C. handler 形态 — _run_incremental_backup 走 PipelineResult/HandlerResult + 磁盘闸批判
"""
import gzip
import os
import sqlite3
import tempfile
from unittest import mock

import pytest


# 被测 handler 走 datetime.now(); 夹具必须与它在时间维度上同源, 否则
# 测试只在被写出来的那一天绿 (Round2 复审实证: 09-15 起 total_rows=2<4)。
_TODAY = __import__("datetime").datetime.now().strftime("%Y-%m-%d")


# ---------- A. 核心模块面 ----------

@pytest.fixture()
def tiny_db(tmp_path):
    """三表临时库: 两表当日有行 (每表≥2行 — GROUP BY/对拍断言判例), 一表当日空."""
    db = tmp_path / "tiny.db"
    con = sqlite3.connect(db)
    # 主键必设: 生产 daily_klines 有 PK (code,date), REPLACE 回放语义
    # 依赖冲突检测 — 无 PK 夹具会让 REPLACE 退化为追加重复行 (夹具贴
    # 生产判例, 2026-09-14 施工亲踩).
    con.executescript(
        """
        CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL,
                                   PRIMARY KEY (code, date));
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, created_at TEXT, msg TEXT);
        CREATE TABLE stock_valuation (code TEXT, date TEXT, pe REAL,
                                     PRIMARY KEY (code, date));
        """
    )
    con.executemany(
        "INSERT INTO daily_klines VALUES (?,?,?)",
        [("300906", "2026-09-14 10:00:00", 29.4), ("600152", "2026-09-14 11:00:00", 8.2)])
    # 时点同源 (Round2 复审): handler 面不传 date, 模块内默认 datetime.now(),
    # 而上面两行钉死在 2026-09-14 —— 那条测试只在写出来的当天是绿的。
    # 这里为"今天"补两行同码不同日的数据 (PK=(code,date) 不撞); 今天恰为
    # 2026-09-14 时下面的 if 不触发, 计数仍是 2, 既有断言一律不动。
    if _TODAY != "2026-09-14":
        con.executemany(
            "INSERT INTO daily_klines VALUES (?,?,?)",
            [("300906", f"{_TODAY} 10:00:00", 29.4),
             ("600152", f"{_TODAY} 11:00:00", 8.2)])
    con.execute("INSERT INTO alerts (id, created_at, msg) VALUES (1, '2026-09-09', 'x')")
    con.execute("INSERT INTO alerts (id, created_at, msg) VALUES (2, '2026-09-10', 'y')")
    # stock_valuation 当日 0 行 (空表路径)
    con.commit()
    con.close()
    return str(db)


def test_run_incremental_backup_dumps_matching_rows(tiny_db, tmp_path):
    from ops_layer.incremental_backup import run_incremental_backup
    bdir = tmp_path / "backups"
    out = run_incremental_backup(tiny_db, str(bdir), date="2026-09-14")
    # daily_klines 两行; alerts 是 FULL_DUMP (全量行) = 2; valuation 当日 0
    assert out["summary"]["daily_klines"] == 2
    assert out["summary"]["alerts"] == 2
    assert out["summary"]["stock_valuation"] == 0
    assert out["total_rows"] == 4
    # 产物: 每表一个 .sql.gz + MANIFEST
    day = bdir / "incremental" / "2026-09-14"
    assert (day / "daily_klines.sql.gz").exists()
    assert (day / "MANIFEST.txt").exists()
    # MANIFEST 记录 md5 与行数
    mf = (day / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "daily_klines: 2 rows" in mf


def test_run_incremental_backup_idempotent_rerun(tiny_db, tmp_path):
    from ops_layer.incremental_backup import run_incremental_backup
    out1 = run_incremental_backup(tiny_db, str(tmp_path / "b"), date="2026-09-14")
    out2 = run_incremental_backup(tiny_db, str(tmp_path / "b"), date="2026-09-14")
    assert out1["total_rows"] == out2["total_rows"]
    # 重跑覆盖同名产物, 不产生 .tmp 残留 (gzip tmp+rename 原子性)
    day = tmp_path / "b" / "incremental" / "2026-09-14"
    leftovers = [p.name for p in day.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_run_incremental_backup_missing_table_no_crash(tiny_db, tmp_path):
    """清单里的表在库中不存在 -> -1 落 summary + warning, 不炸 (增量面 16 表对账)."""
    from ops_layer import incremental_backup as ib
    with mock.patch.object(ib, "INCREMENTAL_TABLES",
                          [("no_such_table", "date", False)]):
        out = ib.run_incremental_backup(tiny_db, str(tmp_path / "b"), date="2026-09-14")
    assert out["summary"]["no_such_table"] == -1
    assert out["total_rows"] == 0


def test_verify_increment_full_match(tiny_db, tmp_path):
    from ops_layer.incremental_backup import (run_incremental_backup,
                                              verify_increment)
    bdir = str(tmp_path / "b")
    run_incremental_backup(tiny_db, bdir, date="2026-09-14")
    rep = verify_increment(tiny_db, os.path.join(bdir, "incremental", "2026-09-14"),
                           date="2026-09-14")
    assert rep["daily_klines"]["count_match"] is True
    assert rep["daily_klines"]["fingerprint_match"] is True
    assert rep["alerts"]["count_match"] is True
    # 空表面 (清单全表可见 — AUD-030 同族防线): 0==0 显式条目
    assert rep["stock_valuation"]["count_match"] is True
    # 报告完整性: 15 表全可见 (无论有没有产物)
    assert set(rep.keys()) == {t for t, _, _ in
                              __import__("ops_layer.incremental_backup",
                                         fromlist=["INCREMENTAL_TABLES"]
                                          ).INCREMENTAL_TABLES}


def test_verify_increment_detects_drift(tiny_db, tmp_path):
    """库在 dump 后**新增**一行 -> count+值面双红 (恢复演练的判定面)."""
    from ops_layer.incremental_backup import (run_incremental_backup,
                                              verify_increment)
    bdir = str(tmp_path / "b")
    run_incremental_backup(tiny_db, bdir, date="2026-09-14")
    con = sqlite3.connect(tiny_db)
    con.execute("INSERT INTO daily_klines VALUES ('000001','2026-09-14 14:00:00', 10.1)")
    con.commit(); con.close()
    rep = verify_increment(tiny_db, os.path.join(bdir, "incremental", "2026-09-14"),
                           date="2026-09-14")
    assert rep["daily_klines"]["count_match"] is False
    assert rep["daily_klines"]["actual"] == 3


def test_verify_increment_catches_update_drift(tiny_db, tmp_path):
    """审计官退场一刀 (AUD-005 反例原样): dump 后 UPDATE 既有行 — 行数不变
    计数面绿, 但值指纹红. 计数对拍会一路绿灯放行旧账 -> 假绿陷阱锁死."""
    from ops_layer.incremental_backup import (run_incremental_backup,
                                              verify_increment)
    bdir = str(tmp_path / "b")
    run_incremental_backup(tiny_db, bdir, date="2026-09-14")
    con = sqlite3.connect(tiny_db)
    # 原形态: AUD-005 回填 = UPDATE exit_price/total_return_pct (非 INSERT)
    con.execute("UPDATE daily_klines SET close=99.9 WHERE code='300906'")
    con.commit(); con.close()
    rep = verify_increment(tiny_db, os.path.join(bdir, "incremental", "2026-09-14"),
                           date="2026-09-14")
    assert rep["daily_klines"]["count_match"] is True      # 计数面照绿
    assert rep["daily_klines"]["fingerprint_match"] is False  # 值面必须红


def test_dump_is_full_column_no_wipe(tiny_db, tmp_path):
    """审计官退场补刀①: REPLACE 语义 = DELETE+INSERT, dump 必须全列 —
    窄列 dump 会把未列出的列静默重置. 本测试锁死: (a) dump 语句列集 ==
    表全列集 (b) 用 dump 回放到全备库后, 行的**未被当日更新过的列**保留原值."""
    import gzip as gz
    import shutil as sh
    from ops_layer.incremental_backup import run_incremental_backup
    bdir = tmp_path / "b"
    run_incremental_backup(tiny_db, str(bdir), date="2026-09-14")
    with gz.open(bdir / "incremental" / "2026-09-14" / "daily_klines.sql.gz",
                 "rt", encoding="utf-8") as f:
        first = next(l for l in f if l.startswith("INSERT"))
    # (a) 列集断言: dump 的 INSERT 列 == 表全列 (SELECT * 派生)
    con = sqlite3.connect(tiny_db)
    all_cols = {r[1] for r in con.execute("PRAGMA table_info(daily_klines)")}
    con.close()
    import re as _re
    m = _re.search(r"INSERT OR REPLACE INTO daily_klines \(([^)]+)\)", first)
    assert m, "column list not found in dump statement"
    dump_cols = {c.strip() for c in m.group(1).split(",")}
    assert dump_cols == all_cols
    # (b) 回放后列值保留: 全备库行 (code/close 同行) 回放 dump 不擦列
    full = tmp_path / "full.db"
    sh.copy(tiny_db, full)  # 行值 == dump 时值 (此场景 dump 即基线)
    rcon = sqlite3.connect(full)
    with gz.open(bdir / "incremental" / "2026-09-14" / "daily_klines.sql.gz",
                 "rt", encoding="utf-8") as f:
        for stmt in f:
            if stmt.startswith("INSERT"):
                rcon.execute(stmt)
    rcon.commit()
    row = rcon.execute(
        "SELECT code, date, close FROM daily_klines WHERE code='300906'"
    ).fetchone()
    rcon.close()
    assert row == ("300906", "2026-09-14 10:00:00", 29.4)  # 全列完好


def test_dump_uses_replace_semantics(tiny_db, tmp_path):
    """回放语义锁死: dump 语句必须是 INSERT OR REPLACE (OR IGNORE 会让
    全备后的 UPDATE 静默丢失 — 审计官退场一刀)."""
    import gzip as gz
    from ops_layer.incremental_backup import run_incremental_backup
    bdir = tmp_path / "b"
    run_incremental_backup(tiny_db, str(bdir), date="2026-09-14")
    with gz.open(bdir / "incremental" / "2026-09-14" / "daily_klines.sql.gz",
                 "rt", encoding="utf-8") as f:
        first = next(l for l in f if l.startswith("INSERT"))
    assert "INSERT OR REPLACE INTO daily_klines" in first
    assert "OR IGNORE" not in first


def test_incremental_tables_census():
    """清单卫生: 全 13 表、无重复、news_stock_links 不在 (无日期列, 归周一全备).

    卡G2 (2026-09-20 用户裁定): popularity_daily/hot_concept_daily 两死表
    停备 (15→13) — 一次性种子 (09-12), 零消费者, 每日备死数据=白做;
    代码保留不通电不退役 (REC 后评估通电)。反向锚: 两表不得回流。
    """
    from ops_layer.incremental_backup import INCREMENTAL_TABLES
    assert len(INCREMENTAL_TABLES) == 13
    names = [t for t, _, _ in INCREMENTAL_TABLES]
    assert len(set(names)) == len(names)
    assert "news_stock_links" not in names
    # 反向锚 (与 test_g2_dead_tables 同语义, 两面同改判例):
    assert "popularity_daily" not in names
    assert "hot_concept_daily" not in names


# ---------- B. 接线三重证 ----------

def test_dispatch_registered_incremental_backup():
    """三重证①调用点: run_pipeline dispatch dict 有 incremental_backup 且映射到新 handler."""
    import ops_layer.orchestrator as orch
    src = open(orch.__file__, encoding="utf-8").read()
    assert '"incremental_backup": self._run_incremental_backup' in src


def test_ops_yaml_has_entry():
    """三重证②入口可达: ops.yaml 管道条目存在, 排 23:05 db_backup 后."""
    import pathlib
    import yaml
    repo = pathlib.Path(__file__).resolve().parents[3]
    raw = yaml.safe_load((repo / "config/defaults/ops.yaml").read_text(encoding="utf-8"))
    pipes = raw["ops"]["pipelines"]
    assert "incremental_backup" in pipes
    entry = pipes["incremental_backup"]
    assert entry["schedule"] == "23:05"
    assert entry["days"] == "1-5"
    # depends_on db_backup -> 顺序面: 增量在每晚全备之后 (全备是当日真源的基线)
    assert "db_backup" in entry["depends_on"]


def test_schedule_check_face_includes_incremental():
    """三重证③落痕有迹: 管道注册面闭环 — dispatch 注册键集与 ops.yaml
    管道键集对拍 (schedule_check 通用对账源, E 席假绿案修复后新管道自动入账)."""
    import pathlib
    import re
    import yaml
    repo = pathlib.Path(__file__).resolve().parents[3]
    raw = yaml.safe_load((repo / "config/defaults/ops.yaml").read_text(encoding="utf-8"))
    names = set(raw["ops"]["pipelines"].keys())
    import ops_layer.orchestrator as orch
    src = open(orch.__file__, encoding="utf-8").read()
    assert "incremental_backup" in names
    # dispatch 与 yaml 同键 (键集差 = 注册面断裂)
    m = re.search(r"dispatch = \{(.+?)\n        \}", src, re.S)
    assert m, "dispatch dict not found"
    dispatch_keys = set(re.findall(r'"([a-z_0-9]+)": self\._run_', m.group(1)))
    # ops.yaml 全部非 continuous 管道应有 dispatch 注册 (缺失=调用点断裂)
    no_handler = names - dispatch_keys
    assert "incremental_backup" not in no_handler


# ---------- C. handler 形态 ----------

def test_handler_contract(tmp_path, tiny_db):
    """handler 返回 HandlerResult; summary 进 metadata; -1 表落 errors 面.
    (清单收窄到夹具三表 — handler 单元测试不绑全清单, 全清单面在
    test_incremental_tables_census + schedule_check 对账.)"""
    from ops_layer.core.models import HandlerResult
    from ops_layer.orchestrator import PipelineOrchestrator
    from ops_layer import incremental_backup as ib
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch.db = mock.Mock()
    orch.db.db_path = tiny_db
    narrow = [t for t in ib.INCREMENTAL_TABLES
              if t[0] in {"daily_klines", "alerts", "stock_valuation"}]
    with mock.patch.object(orch, "_cfg_all",
                          return_value=str(tmp_path / "backups")),          mock.patch.object(ib, "INCREMENTAL_TABLES", narrow):
        result = orch._run_incremental_backup({"date": "2026-09-14"})
    assert isinstance(result, HandlerResult)
    assert result.success is True
    assert result.metadata["total_rows"] >= 4
    assert result.metadata["summary"]["daily_klines"] == 2


def test_handler_defaults_to_today_when_no_date(tmp_path, tiny_db):
    """缺省无 date -> 用当日 (bj_date) — 生产行为零变更锚 (2026-09-16 修复的回归面)."""
    from ops_layer.core.models import HandlerResult
    from ops_layer.orchestrator import PipelineOrchestrator
    from ops_layer import incremental_backup as ib
    from unittest import mock
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch.db = mock.Mock()
    orch.db.db_path = tiny_db
    narrow = [t for t in ib.INCREMENTAL_TABLES
              if t[0] in {"daily_klines", "alerts", "stock_valuation"}]
    with mock.patch.object(orch, "_cfg_all",
                          return_value=str(tmp_path / "backups")),          mock.patch.object(ib, "INCREMENTAL_TABLES", narrow):
        result = orch._run_incremental_backup({})
    # 夹具数据钉死 09-14; 缺省取"当日" -> 断言的是"走了缺省路径且未炸",
    # 不断言具体日期 (日期无关), 且 metadata 回带 date.
    assert isinstance(result, HandlerResult)
    assert "date" in result.metadata

def test_handler_missing_db_returns_failed(tmp_path):
    """db 路径不存在 -> success=False errors 非空 (跳过≠成功 铁律#12)."""
    from ops_layer.orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch.db = mock.Mock()
    orch.db.db_path = str(tmp_path / "nope.db")
    with mock.patch.object(orch, "_cfg_all", return_value=str(tmp_path / "b")):
        result = orch._run_incremental_backup({})
    assert result.success is False
    assert result.errors


# ---------- D. 恢复演练终判面 (verify_restore) ----------

def test_verify_restore_replay_catches_update(tmp_path, tiny_db):
    """恢复演练核心场景 (审计官 HANDOVER 约束+退场一刀):
    全备 -> 次日增量 (含 UPDATE 行) -> 文件复制恢复 -> REPLACE 回放 -> 值面对拍.
    演练判绿必须以恢复库值面 == 生产库为准, INSERT OR IGNORE 在此必红."""
    import shutil as sh
    from ops_layer.incremental_backup import (run_incremental_backup,
                                              verify_restore)
    # live = tiny_db; "全备" = 复制 live 为基线库 (文件复制, 不走 DDL 重建)
    full = tmp_path / "full_base.db"
    sh.copy(tiny_db, full)
    # live 在全备后发生 UPDATE (AUD-005 回填形态: 值改行数不变)
    con = sqlite3.connect(tiny_db)
    con.execute("UPDATE daily_klines SET close=31.2 WHERE code='300906'")
    con.commit(); con.close()
    # 增量 dump 晚间跑 (捕住 UPDATE 后的行)
    bdir = str(tmp_path / "b")
    run_incremental_backup(tiny_db, bdir, date="2026-09-14")
    # 恢复: 基线库 + REPLACE 回放
    restored = tmp_path / "restored.db"
    sh.copy(full, restored)
    rcon = sqlite3.connect(restored)
    for gz_fn in (tmp_path / "b" / "incremental" / "2026-09-14").glob("*.sql.gz"):
        import gzip as gz
        with gz.open(gz_fn, "rt", encoding="utf-8") as f:
            for stmt in f:
                if stmt.startswith("INSERT"):
                    rcon.execute(stmt)
    rcon.commit(); rcon.close()
    # 终判: 恢复库 vs live 值面对拍 (REPLACE 语义下 UPDATE 行已覆盖 -> 双绿)
    rep = verify_restore(tiny_db, str(restored), dates=["2026-09-14"])
    assert rep["daily_klines"]["count_match"] is True
    assert rep["daily_klines"]["fingerprint_match"] is True


def test_verify_restore_flags_stale_value(tmp_path, tiny_db):
    """反例锁死: 若回放用 OR IGNORE (UPDATE 丢失), count 照绿值面红 —
    verify_restore 必须把这个库判为恢复失败 (假绿陷阱的判定面)."""
    import shutil as sh
    from ops_layer.incremental_backup import verify_restore
    full = tmp_path / "full_base.db"
    sh.copy(tiny_db, full)
    con = sqlite3.connect(tiny_db)
    con.execute("UPDATE daily_klines SET close=31.2 WHERE code='300906'")
    con.commit(); con.close()
    # 恢复库 = 只有全备, 无回放 (模拟 IGNORE 丢 UPDATE 的形态)
    restored = tmp_path / "restored_no_replay.db"
    sh.copy(full, restored)
    rep = verify_restore(tiny_db, str(restored), dates=["2026-09-14"])
    assert rep["daily_klines"]["count_match"] is True      # 行数没变
    assert rep["daily_klines"]["fingerprint_match"] is False  # 值还是旧账
