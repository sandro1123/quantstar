# -*- coding: utf-8 -*-
"""marimo 页跨格下划线泄漏守卫（#76 周报预演返工件，09-02 沙箱真跑实证）。

预演实证：p6_ops 渲染格 f-string 引用上一格定义的 `_runs_win`
→ MarimoExceptionRaisedError: Name '_runs_win' is not defined（marimo 语义：
下划线名=格私有，不跨格注入，跨格引用必 NameError）。pytest 全绿仍炸
export——marimo 页不在 pytest 执行面，此类病是单测天然盲区。

本守卫 AST 静态扫描 scripts/marimo_pages/p*.py 全部 @app.cell：凡 Load
引用的下划线名（非双下划线）在本格未绑定（非形参/赋值/import/except-as/
内嵌函数形参），即红。防同族复发（p0~p6 全页覆盖）。
"""
import ast
from pathlib import Path

PAGES_DIR = Path(__file__).resolve().parents[3] / "scripts" / "marimo_pages"


def _cell_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if ((isinstance(dec, ast.Attribute) and dec.attr == "cell")
                        or (isinstance(dec, ast.Name) and "cell" in dec.id)):
                    yield node
                    break


def _bound_names(fn):
    bound = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            for a in (list(node.args.args)
                      + list(getattr(node.args, "posonlyargs", []))
                      + list(node.args.kwonlyargs)):
                bound.add(a.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def _leaks(fn):
    bound = _bound_names(fn)
    for node in ast.walk(fn):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id.startswith("_") and not node.id.startswith("__")
                and node.id not in bound):
            yield node.id, node.lineno


def test_all_pages_no_cross_cell_underscore_leak():
    pages = sorted(PAGES_DIR.glob("p*.py"))
    assert len(pages) >= 7, f"marimo 页缺失: {[p.name for p in pages]}"
    violations = []
    for page in pages:
        tree = ast.parse(page.read_text(encoding="utf-8"), filename=str(page))
        for fn in _cell_functions(tree):
            for name, lineno in _leaks(fn):
                violations.append(f"{page.name}:{lineno} `{name}`")
    assert not violations, (
        "marimo 页跨格下划线泄漏（下划线名=格私有，跨格引用 export 时必 "
        f"NameError，pytest 盲区）:\n  " + "\n  ".join(violations))


def test_detector_catches_synthetic_leak():
    """守卫自证：合成病页必须被检出（防守卫退化成永绿摆设）。"""
    src = (
        "import marimo\n"
        "app = marimo.App()\n"
        "\n"
        "@app.cell\n"
        "def _(C):\n"
        "    _w = C.window_days(13)\n"
        "    X = _w + 1\n"
        "    return (X,)\n"
        "\n"
        "@app.cell\n"
        "def _(X, mo):\n"
        "    mo.md(f'win {_w}')\n"
    )
    tree = ast.parse(src)
    leaks = {name for fn in _cell_functions(tree) for name, _ in _leaks(fn)}
    assert "_w" in leaks, "守卫未检出合成泄漏——探测器失效"
