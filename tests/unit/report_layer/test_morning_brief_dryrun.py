"""W1 任务D: 晨报 dry-run 零副作用 — build_positions 不得触发 el.daily_update()。

病: scripts/build_morning_brief.py:206 的 build_positions() 无条件
el.start() + el.daily_update() (写库), main() 的 --dry-run 只拦推送不拦写入
→ 08:05 的 --dry-run 调试跑每晨对生产库做一次退出后检查+台账写。
"""
from unittest.mock import MagicMock

import pytest

import scripts.build_morning_brief as mb


class _EL:
    instances = []

    def __init__(self, called):
        self._called = called

    def start(self): pass
    def shutdown(self): pass
    def daily_update(self): self._called["daily_update"] += 1
    def get_portfolio_snapshot(self): return []
    def filter_trades(self, status=None, limit=None): return []


def _patch_common(monkeypatch, called):
    monkeypatch.setattr("scripts.build_morning_brief.Database", MagicMock())
    monkeypatch.setattr(mb, "_preview_execution_db", MagicMock())
    monkeypatch.setattr("scripts.build_morning_brief.Config", MagicMock())
    monkeypatch.setattr("scripts.build_morning_brief.ExecutionLayer",
                        lambda **kw: _EL(called))
    monkeypatch.setattr("scripts.build_morning_brief.StrategyLayer", MagicMock())
    data = MagicMock()
    data.get_stock_info.return_value = None
    monkeypatch.setattr("scripts.build_morning_brief.DataLayer",
                        MagicMock(return_value=data))


def test_build_positions_runs_daily_update_by_default(monkeypatch):
    """正测锚: 真路径下 daily_update 会被调 (证明 mock 挂对了对象)。"""
    called = {"daily_update": 0}
    _patch_common(monkeypatch, called)
    out = mb.build_positions()
    assert out is not None
    assert called["daily_update"] == 1


def test_build_positions_dry_run_skips_daily_update(monkeypatch):
    """RED→GREEN: dry-run 时不得执行 daily_update 写副作用。"""
    called = {"daily_update": 0}
    _patch_common(monkeypatch, called)
    out = mb.build_positions(dry_run=True)
    assert out is not None
    assert called["daily_update"] == 0, "dry-run 不得执行 daily_update 写副作用"


def test_main_dry_run_reaches_positions_without_daily_update(monkeypatch, tmp_path):
    """CLI→构建→持仓走真实调用链，仅隔离外部数据与执行层对象。"""
    called = {"daily_update": 0}
    _patch_common(monkeypatch, called)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mb.sys, "argv", ["build_morning_brief.py", "--dry-run"])
    monkeypatch.setattr(mb, "build_overnight", lambda: {})
    monkeypatch.setattr(mb, "build_news", lambda date, dry_run=False: [])
    monkeypatch.setattr(mb, "build_indices", lambda: [])
    monkeypatch.setattr(mb, "build_stock_picks", lambda dry_run=False: [])
    monkeypatch.setattr(mb, "build_market_outlook", lambda indices, dry_run=False: None)
    delivery = MagicMock()
    monkeypatch.setattr(mb, "DeliveryLayer", delivery)
    positions = MagicMock(wraps=mb.build_positions)
    monkeypatch.setattr(mb, "build_positions", positions)

    mb.main()

    positions.assert_called_once_with(dry_run=True)
    assert positions.return_value is not None
    assert called["daily_update"] == 0
    delivery.assert_not_called()
    assert (tmp_path / "output" / "preview" / f"morning_brief_{mb.DATE}.txt").is_file()


@pytest.mark.parametrize("as_list", [True, False])
def test_main_live_sends_once_and_reports_first_result(monkeypatch, tmp_path, capsys, as_list):
    """单结果/列表两种契约都只发一次，且展示这一次的结果。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mb.sys, "argv", ["build_morning_brief.py"])
    monkeypatch.setattr(mb, "build_morning_brief", lambda *a, **kw: {"sections": []})
    monkeypatch.setattr(mb, "Config", MagicMock())
    monkeypatch.setattr(mb, "FeishuCardFormatter", MagicMock())
    result = MagicMock(channel="first-channel", status="success")
    delivery = MagicMock()
    delivery.send.return_value = [result] if as_list else result
    monkeypatch.setattr(mb, "DeliveryLayer", MagicMock(return_value=delivery))

    mb.main()

    delivery.send.assert_called_once()
    assert "推送 [first-channel]: success" in capsys.readouterr().out
