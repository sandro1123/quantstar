# -*- coding: utf-8 -*-
"""D0 卡 宇宙漂移哨兵 universe_sentinel — RED 先行验收测试 (mock + :memory:, 无网络)。

哨兵判据 (派单口径 sm-10fc62f9 + 普查件 d0_census_draft.md):
  - 日行数环比跌幅 > 2% (阈值 UNIVERSE_DROP_PCT=2.0) → 疑似台阶
  - 基线 = 前 5 交易日均值 (非前一日, 防单日异常污染基线)
  - 瞬态锯齿 (隔日自愈) 与永久台阶区分: T 日跌 → T+1 回弹 ≥ 基线95% = 锯齿轻提示;
    T+1 未回弹 = 台阶正式告警
形态: 09:30 轻哨同族 (freshness_sentinel #116) — cron 直驱, ro 连接零写库,
  飞书 best-effort 失败静默。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))            # scripts/ 直驱形态
sys.path.insert(0, str(_root / "src"))    # src 包导入形态

import scripts.universe_sentinel as us


class _FakeDB:
    """:memory: 库 + 只建 factor_daily (哨兵只读此表的按日行数)。"""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE factor_daily (date TEXT, code TEXT)")
        self.conn.commit()

    def set_counts(self, counts: dict):
        """{date: n} → 每日 n 行 code0..n-1。"""
        for d, n in counts.items():
            self.conn.executemany(
                "INSERT INTO factor_daily VALUES (?, ?)",
                [(d, f"9{i:05d}") for i in range(n)])
        self.conn.commit()

    def count_on(self, date: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE date=?", (date,)).fetchone()[0]

    def dates(self) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT date FROM factor_daily ORDER BY date")]


@pytest.fixture
def fdb(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(us, "_connect_ro", lambda: db.conn)
    return db


def _run(d, notify_disabled=True):
    return us.run(d, notify_disabled=notify_disabled)


# --- 阈值与基线口径 ---

def test_threshold_constant():
    """环比跌幅阈值 = 2%。"""
    assert us.UNIVERSE_DROP_PCT == 2.0


def test_baseline_is_5day_mean(fdb):
    """基线 = 前 5 交易日均值 (非前一日): 前一日被锯齿打低时基线不被污染。"""
    counts = {
        "2026-09-01": 5800, "2026-09-02": 5800, "2026-09-03": 5800,
        "2026-09-04": 5800, "2026-09-07": 5800,   # 前 5 日均值 5800
        "2026-09-08": 5400,                        # 当日 -6.9% 台阶
    }
    fdb.set_counts(counts)
    base = us.baseline_for(fdb.conn, "2026-09-08")
    assert base == 5800  # 前一日 5800 正常, 基线 = 前5日含 09-04 的均值


def test_baseline_robust_to_sawtooth_day_in_window(fdb):
    """基线窗口含锯齿日 (异常低行日) 时不被拉低 — 中位数稳健口径。"""
    counts = {
        "2026-09-01": 5800, "2026-09-02": 5800, "2026-09-03": 5800,
        "2026-09-04": 5800,
        "2026-09-07": 5400,   # 窗口内锯齿日
        "2026-09-08": 5400,
    }
    fdb.set_counts(counts)
    base = us.baseline_for(fdb.conn, "2026-09-08")
    assert base == 5800  # 中位数口径: 单个锯齿日不拉低基线


# --- 判读面 ---

def test_normal_day_no_alert(fdb, capsys):
    counts = {"2026-08-31": 5800}
    counts.update({f"2026-09-0{i}": 5800 for i in range(1, 5)})
    counts.update({"2026-09-07": 5800, "2026-09-08": 5790})
    fdb.set_counts(counts)
    rc = _run("2026-09-08")
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out and "台阶" not in out and "锯齿" not in out


def test_step_down_alerts(fdb, capsys):
    """永久台阶: 跌幅 >2% 且次日未回弹 → 正式告警 rc=1。"""
    counts = {
        "2026-09-01": 5800, "2026-09-02": 5800, "2026-09-03": 5800,
        "2026-09-04": 5800, "2026-09-07": 5800,
        "2026-09-08": 5400,          # -6.9% 台阶日
        "2026-09-09": 5400,          # T+1 未回弹 → 永久台阶
    }
    fdb.set_counts(counts)
    rc = _run("2026-09-09")
    out = capsys.readouterr().out
    assert rc == 1
    assert "台阶" in out or "漂移" in out
    assert "2026-09-08" in out       # 告警体点名台阶起始日


def test_sawtooth_self_healed_gentle(fdb, capsys):
    """瞬态锯齿: T 跌 >2%, T+1 回弹 ≥ 基线99% → 轻提示不告警。"""
    counts = {
        "2026-09-01": 5800, "2026-09-02": 5800, "2026-09-03": 5800,
        "2026-09-04": 5800, "2026-09-07": 5800,
        "2026-09-08": 5300,          # -8.6% 锯齿日
        "2026-09-09": 5780,          # T+1 回弹 ≥ 99%×5800=5742
    }
    fdb.set_counts(counts)
    rc = _run("2026-09-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "锯齿" in out and "台阶" not in out


def test_step_alerts_when_rebound_face_not_landed(fdb, capsys):
    """晨跑形态: T 日行未落库 (n_t=0) 时跌 >2% → 疑似台阶告警 (待复判), 不静默。"""
    counts = {
        "2026-08-31": 5800, "2026-09-01": 5800, "2026-09-02": 5800,
        "2026-09-03": 5800, "2026-09-04": 5800, "2026-09-07": 5800,
        "2026-09-08": 5400,          # -6.9% 台阶日
        # 2026-09-09 无行 (晨跑形态: 当日 factor_update 16:00 才落库)
    }
    fdb.set_counts(counts)
    rc = _run("2026-09-09")
    out = capsys.readouterr().out
    assert rc == 1
    assert "待复判" in out
    assert "2026-09-08" in out


def test_empty_history_gentle(fdb, capsys):
    """无历史 (新装机/表空) → 轻提示 rc=0, 不告警不炸。"""
    fdb.set_counts({"2026-09-08": 5500})
    rc = _run("2026-09-08")
    out = capsys.readouterr().out
    assert rc == 0
    assert "不足" in out or "无历史" in out


def test_resolve_db_path_falls_back_when_cwd_db_missing(monkeypatch):
    """Database() 解析出不存在路径 (worktree 形态) → 存在性回落生产路径。"""
    import os
    monkeypatch.setattr("common.database.Database",
                        lambda: type("X", (), {"db_path": "/nonexistent/qs.db"})())
    prod = os.path.expanduser("~/qs/data/qs.db")
    got = us._resolve_db_path()
    if os.path.exists(prod):
        # 设备形态: 生产库存在 → 回落到生产路径
        assert got == prod or got.endswith("data/qs.db")
    else:
        # 本地无库形态: 返回链尾 (诚实), 不抛
        assert isinstance(got, str)


def test_second_step_after_crawl_not_masked(fdb, capsys):
    """基线爬行 RED: 台阶落地后低行数日混入基线窗口 → 中位基线下移吞掉二次台阶。
    复现 08-10 真实回放: 扩容日 5830 后跌至 5538 (-5.0% vs 5830),
    但窗口 (5657×4+5830) 中位=5657 → 只报 -2.1% 不告警 = 病。
    修法: 跌幅判据须对 **峰值基线** (窗口最大值) 也过阈值 — 任一口径过阈即疑似。"""
    counts = {
        "2026-08-03": 5657, "2026-08-04": 5657, "2026-08-05": 5657,
        "2026-08-06": 5657, "2026-08-07": 5830,   # 扩容日
        "2026-08-10": 5538,                        # vs 峰值 5830 = -5.0%, vs 中位 5657 = -2.1%
        "2026-08-11": 5539,                        # 未回弹到 5830×0.95=5538.5 → 未回弹
    }
    fdb.set_counts(counts)
    rc = _run("2026-08-11")
    out = capsys.readouterr().out
    assert rc == 1
    assert "台阶" in out or "待复判" in out
