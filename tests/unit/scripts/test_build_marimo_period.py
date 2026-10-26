"""#76 marimo 周报/月报施工：--period 周期分型测试（TDD 红先行）。

对统筹承诺口径（五件）+ 两条通道测试：
  1) daily 向后兼容：默认 period=daily；归档路径 / content_type / DM 标题与现状逐字一致
  2) 周期分型：weekly/monthly 归档 reports/html/{period}/{asof}/；
     content_type 对齐 ops.yaml 既有管道名 marimo_weekly_report / marimo_monthly_report
  3) --date 显式日：与 --period 正交
  4) 幂等重跑：同 (period, asof) 归档路径一致（路径无时间戳成分，重跑覆盖同目录）
  5) --pages 正交：子集选择与周期正交；未知页仍拒
  6) export 通道：MARIMO_REPORT_PERIOD 注入子进程环境且不污染当前进程
  7) _common 通道：report_period / window_days 周期感知；未知值回落 daily
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "marimo_pages"))

import build_marimo_report as B  # noqa: E402
import _common as C  # noqa: E402


# --- 1) daily 向后兼容（现网四行 crontab 不带 --period，必须零行为变化） ----
def test_daily_default_backward_compatible():
    args = B.parse_args([])
    assert args.period == "daily"
    out = B.outdir_for(args.period, "2026-08-31")
    assert out == B.QS / "reports" / "html" / "daily" / "2026-08-31"
    assert B.content_type_for("daily") == "marimo_report"
    assert B.dm_title_for("daily", "2026-08-31") == "📊 QS 每日深钻报告 2026-08-31"


# --- 2) 周期分型 ----------------------------------------------------------
def test_period_typing_weekly():
    args = B.parse_args(["--period", "weekly"])
    assert args.period == "weekly"
    out = B.outdir_for("weekly", "2026-08-31")
    assert out == B.QS / "reports" / "html" / "weekly" / "2026-08-31"
    assert B.content_type_for("weekly") == "marimo_weekly_report"
    assert "周报" in B.dm_title_for("weekly", "2026-08-31")


def test_period_typing_monthly():
    args = B.parse_args(["--period", "monthly"])
    assert args.period == "monthly"
    out = B.outdir_for("monthly", "2026-08-31")
    assert out == B.QS / "reports" / "html" / "monthly" / "2026-08-31"
    assert B.content_type_for("monthly") == "marimo_monthly_report"
    assert "月报" in B.dm_title_for("monthly", "2026-08-31")


def test_bad_period_rejected():
    with pytest.raises(SystemExit):
        B.parse_args(["--period", "yearly"])


# --- 3) --date 显式日与周期正交 -------------------------------------------
def test_date_explicit_orthogonal():
    args = B.parse_args(["--period", "weekly", "--date", "2026-08-29"])
    assert args.date == "2026-08-29"
    assert args.period == "weekly"
    out = B.outdir_for(args.period, args.date)
    assert out.name == "2026-08-29"
    assert out.parent.name == "weekly"


# --- 4) 幂等重跑 ----------------------------------------------------------
def test_idempotent_rerun_same_paths():
    a1 = B.outdir_for("monthly", "2026-08-31")
    a2 = B.outdir_for("monthly", "2026-08-31")
    assert a1 == a2
    rel = a1.relative_to(B.QS / "reports" / "html")
    assert rel.parts == ("monthly", "2026-08-31")


# --- 5) --pages 正交 -------------------------------------------------------
def test_pages_subset_orthogonal():
    args = B.parse_args(["--pages", "p0_overview,p3_performance",
                         "--period", "monthly"])
    assert args.pages == "p0_overview,p3_performance"
    assert args.period == "monthly"


def test_pages_unknown_still_rejected():
    with pytest.raises(SystemExit):
        B.parse_args(["--pages", "pX_bogus"])


# --- 6) export 子进程环境通道 ----------------------------------------------
def test_export_env_injects_period(monkeypatch):
    monkeypatch.delenv("MARIMO_REPORT_PERIOD", raising=False)
    env = B.export_env_for("weekly")
    assert env["MARIMO_REPORT_PERIOD"] == "weekly"
    # 只进子进程环境副本，不污染当前进程
    import os
    assert os.environ.get("MARIMO_REPORT_PERIOD") is None
    # 基于当前环境扩展而非清空（PATH 等仍在）
    assert env.get("PATH") == os.environ.get("PATH")


# --- 7) _common 页窗口周期感知通道 ----------------------------------------
def test_common_report_period_default(monkeypatch):
    monkeypatch.delenv("MARIMO_REPORT_PERIOD", raising=False)
    assert C.report_period() == "daily"
    assert C.window_days(7) == 7
    assert C.window_days(30) == 30


def test_common_window_days_weekly(monkeypatch):
    monkeypatch.setenv("MARIMO_REPORT_PERIOD", "weekly")
    assert C.report_period() == "weekly"
    assert C.window_days(7) == 7      # 周窗=7：不变
    assert C.window_days(3) == 7      # 小于周期窗：抬到 7
    assert C.window_days(30) == 30    # 大窗不动


def test_common_window_days_monthly(monkeypatch):
    monkeypatch.setenv("MARIMO_REPORT_PERIOD", "monthly")
    assert C.report_period() == "monthly"
    assert C.window_days(7) == 30     # 月报：7 抬到 30
    assert C.window_days(14) == 30
    assert C.window_days(40) == 40    # 已超周期窗：不动


def test_common_unknown_period_falls_back_daily(monkeypatch):
    monkeypatch.setenv("MARIMO_REPORT_PERIOD", "bogus")
    assert C.report_period() == "daily"
    assert C.window_days(7) == 7
