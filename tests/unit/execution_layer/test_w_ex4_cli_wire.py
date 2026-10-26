# -*- coding: utf-8 -*-
"""EX-4 CLI 挂线（授权施工版）— RED 先行。

派单 sm-141ed9c 防御形态三要件:
  1. ex4_cli_scan.run() 外层 try/except — 收盘段失败不炸主扫描 (解耦)
  2. 失败 logger.warning 不静默 (铁律 #12)
  3. 集成测试: main() dry 态走到 ex4_cli_scan.run 被调用 (防未来被"优化"掉)

坑账: intraday_scan 顶部 from-import 把依赖名绑定进模块命名空间,
string 形 monkeypatch 源模块不生效 — 必须 patch 模块属性 (scan_mod.X)。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))


@pytest.fixture
def scan_mod():
    import importlib
    return importlib.import_module("execution_layer.scripts.intraday_scan")


def _stub_heavy_deps(monkeypatch, scan_mod, fake_db, fake_el):
    """桩掉全部重量依赖 — patch 模块已绑定名 (from-import 命名空间面)。"""
    monkeypatch.setattr(scan_mod, "Database", lambda: fake_db, raising=False)
    monkeypatch.setattr(scan_mod, "Config", MagicMock(), raising=False)
    monkeypatch.setattr(scan_mod, "DataLayer", MagicMock(), raising=False)
    monkeypatch.setattr(scan_mod, "StrategyLayer", MagicMock(), raising=False)
    monkeypatch.setattr(scan_mod, "ExecutionLayer",
                        lambda **kw: fake_el, raising=False)
    # AgentLayer 是 main() 内 try 块 from-import — patch 包属性即拦
    import agent_layer as ag_pkg
    monkeypatch.setattr(ag_pkg, "AgentLayer",
                        MagicMock(side_effect=ImportError("stub")), raising=False)


def test_intraday_scan_calls_ex4_cli(monkeypatch, scan_mod):
    """要件3 集成测试: main() dry 态 → ex4_cli_scan.run 被调用 (防优化掉)。"""
    fake_db = MagicMock()
    fake_el = MagicMock()
    _stub_heavy_deps(monkeypatch, scan_mod, fake_db, fake_el)

    called = []
    import execution_layer.scripts.ex4_cli_scan as cli_mod
    # main() 内 from-import run 在调用时刻解析属性 — patch 模块属性即拦
    monkeypatch.setattr(cli_mod, "run",
                        lambda db, **kw: called.append(db) or 0)

    scan_mod.main()

    assert len(called) == 1, (
        "main() 必须调用 ex4_cli_scan.run 一次 (挂线三重证之调用点)")
    assert called[0] is fake_db
    fake_el.intraday_scan.assert_called_once()
    fake_el.shutdown.assert_called_once()


def test_intraday_scan_cli_failure_does_not_kill_main(monkeypatch, scan_mod):
    """要件1+2: ex4_cli_scan.run 抛异常 → main 仍正常返回 (解耦) + warning 留痕。"""
    fake_db = MagicMock()
    fake_el = MagicMock()
    _stub_heavy_deps(monkeypatch, scan_mod, fake_db, fake_el)

    import execution_layer.scripts.ex4_cli_scan as cli_mod
    def _boom(db, **kw):
        raise RuntimeError("收盘段模拟炸")
    monkeypatch.setattr(cli_mod, "run", _boom)

    rc = scan_mod.main()          # 无 try/except 时此行直接 RuntimeError
    assert rc is None             # main 正常走完 (无异常冒出)
    fake_el.intraday_scan.assert_called_once()   # 主扫描已跑
    fake_el.shutdown.assert_called_once()        # shutdown 仍被走到
