# -*- coding: utf-8 -*-
"""跨域污染守卫 (09-12 根治案 sm-be69213): 禁测试硬编码他树/worktree 绝对路径。

病根形态: test_tushare_wallclock.py 施工期把 qs-ddpfix 硬编码进
sys.path.insert(0) — 合跑时后续域 (memory_layer wire 收集炸/agent 错树 re-import)
全污染。单域绿/合跑炸 = 时敏污染家族第 7 形态 (跨域 sys.path/sys.modules 状态污染)。

守卫语义: tests/ 树内任何 .py 文件出现他树 worktree 绝对路径字面量即红 —
测试路径必须 __file__ 相对推导, 在哪个树跑就解析哪个树 (AG-WIRE-1 判例工程化)。
"""
import os
import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[2] / "tests"
_SELF_NAME = Path(__file__).resolve().name
_NL = chr(10)

_FORBIDDEN = re.compile(ros.path.expanduser("~/qs-[a-z0-9]+"))


def test_no_hardcoded_foreign_worktree_paths():
    offenders = []
    for p in _TESTS.rglob("*.py"):
        if p.name == _SELF_NAME:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _FORBIDDEN.finditer(text):
            line_no = text[: m.start()].count(_NL) + 1
            offenders.append(str(p.relative_to(_TESTS)) + ":" + str(line_no) + ": " + m.group(0))
    assert not offenders, (
        "测试树内硬编码他 worktree 绝对路径 (跨域污染源, 判例见 "
        "test_tushare_wallclock docstring):" + _NL + _NL.join(offenders))
