"""OPS-03: 部署门禁 — 三种检查各有失败用例 + 干净通过

用临时目录构造最小仓库结构, 测门禁行为边界:
- 冲突标记注入 → 拦截
- 语法错误注入 → compileall 拦截
- bootstrap 不可导入 → smoke 拦截
- 干净结构 → 通过
- 旁路环境变量 → 放行
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")))
from deploy_gate import (  # noqa: E402
    check_bootstrap_import,
    check_compileall,
    check_conflict_markers,
    run_gate,
)


def _make_repo(tmp_path, bootstrap_body="X = 1\n"):
    """构造最小仓库: src/bootstrap.py + src/pkg/mod.py"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "bootstrap.py").write_text(bootstrap_body, encoding="utf-8")
    pkg = src / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("Y = 2\n", encoding="utf-8")
    return tmp_path


def test_clean_repo_passes(tmp_path):
    repo = _make_repo(tmp_path)
    assert run_gate(root=str(repo), alert=False) == 0


def test_conflict_marker_blocks(tmp_path):
    repo = _make_repo(tmp_path)
    # 注入冲突标记 (模拟合并残留)
    (repo / "src" / "pkg" / "mod.py").write_text(
        "Y = 2\n<<<<<<< HEAD\nZ = 3\n=======\nZ = 4\n>>>>>>> fix/batch\n", encoding="utf-8")
    ok, detail = check_conflict_markers(str(repo))
    assert not ok
    assert "mod.py" in detail
    assert run_gate(root=str(repo), alert=False) == 1


def test_syntax_error_blocks_compileall(tmp_path):
    repo = _make_repo(tmp_path, bootstrap_body="def broken(:\n    pass\n")
    ok, detail = check_compileall(str(repo), sys.executable)
    assert not ok
    assert "compileall" in detail
    assert run_gate(root=str(repo), alert=False) == 1


def test_bootstrap_import_failure_blocks(tmp_path):
    repo = _make_repo(tmp_path, bootstrap_body="import nonexistent_module_xyz\n")
    ok, detail = check_bootstrap_import(str(repo), sys.executable)
    assert not ok
    assert "import bootstrap" in detail
    assert run_gate(root=str(repo), alert=False) == 1


def test_skip_env_bypasses(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, bootstrap_body="def broken(:\n")
    monkeypatch.setenv("QS_DEPLOY_GATE_SKIP", "1")
    assert run_gate(root=str(repo), alert=False) == 0


def test_marker_in_tests_dir_ignored(tmp_path):
    """tests/ 下 fixture 合法包含标记字面量, 不应误报。"""
    repo = _make_repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "fixture.py").write_text("S = '<<<<<<< HEAD'\n", encoding="utf-8")
    ok, _ = check_conflict_markers(str(repo))
    assert ok
