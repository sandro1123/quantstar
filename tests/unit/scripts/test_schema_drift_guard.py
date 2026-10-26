# -*- coding: utf-8 -*-
"""件1 可执行断言 —— schema 漂移的防复发守卫。

统筹派单点名要「可执行断言」。一次性跑出一份「零 P1」的报告**不是交付**:
下次有人给 `src` 的 DDL 加一条生产不满足的 CHECK, 没有任何东西会红。
本判例是我自己写下的: **判例写下来不等于内化, 真正管用的是机器可执行的断言。**

两层, 合起来才算数:

【第一层 · 守卫的守卫】(始终运行)
    造带**已知漂移**的假库, 断言工具真的把它们逐类抓出来。
    没有这一层, 「零 P1」在工具坏掉时是**空转恒真** —— 而工具坏掉过一次:
    非贪婪正则在 `IN ('a','b')` 内层括号截断, 表达式跑不动。
    那次是「探针错 != 通过」的守卫兜住的, 但守卫本身当时也没有测试。

【第二层 · 生产实况】(生产库在时运行, 缺席时**响亮跳过**)
    对真生产库断言 P1 == 0 且 P2 == 0。
    跳过必须留痕: 静默跳过是本轮审计一路在治的病, 不能长在自己的守卫里。
"""
from __future__ import annotations

import os

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_drift_audit as sda  # noqa: E402

# 生产库候选路径。**写成清单而不是单值**, 免得换了位置就永远静默跳过。
PROD_CANDIDATES = [
    Path.home() / "qs" / "data" / "qs.db",
    Path(os.path.expanduser("~/qs/data/qs.db")),
]


def _mk(sql_list) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for s in sql_list:
        con.executescript(s)
    con.commit()
    return con


# ============================================================ 第一层: 守卫的守卫

def test_identical_schema_reports_no_drift():
    """反面锚: 两侧完全一致时必须**什么都不报**。

    先钉死这一条, 后面每条「能抓到」才有意义 ——
    否则一个「什么都报」的坏工具也能让所有正面断言通过。
    """
    ddl = "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)"
    prod, code = _mk([ddl]), _mk([ddl])
    d = sda.compare_table(prod, code, "t")
    assert not any(d.values()), f"一致的表被报成有差异: {d}"
    assert sda.severity("t", d, 0) == ("", "")


def test_column_missing_in_code_is_p1():
    """生产有·代码不建的列 = 回灌必报 no such column = P1。"""
    prod = _mk(["CREATE TABLE t (id INTEGER, extra TEXT)"])
    code = _mk(["CREATE TABLE t (id INTEGER)"])
    d = sda.compare_table(prod, code, "t")
    assert d["missing_in_code"] == ["extra"]
    assert sda.severity("t", d, 10)[0] == "P1"


def test_type_and_notnull_drift_are_detected():
    """类型 / NOT NULL 分别落在各自的面上, 不许串面。"""
    prod = _mk(["CREATE TABLE t (id INTEGER, a TEXT, b INTEGER)"])
    code = _mk(["CREATE TABLE t (id INTEGER, a INTEGER, b INTEGER NOT NULL)"])
    d = sda.compare_table(prod, code, "t")
    assert any("a:" in x for x in d["type_drift"]), "类型漂移漏抓"
    assert any("b:" in x for x in d["notnull_drift"]), "NOT NULL 漂移漏抓"
    assert sda.severity("t", d, 10)[0] == "P2", "NOT NULL 漂移至少 P2"


# ------------------------------------------------- 本件的核心判据: 有约束 != 会拒数据

def test_check_satisfied_by_data_is_p3_not_p1():
    """**有约束 != 会拒数据。**

    这是本件我自我纠正的第一处: 首版把「代码独有 CHECK」直接判 P1,
    实打后 5 条全过 —— 其中 `action_records` 那条正是我自己 AUD-028 扩到
    33 项修好的。按首版交差, 我会把**已修好的东西**当 P1 报给统筹。
    判据面 != 事实面, 这次犯在自己的工具里。
    """
    prod = _mk(["CREATE TABLE t (id INTEGER, kind TEXT)",
                "INSERT INTO t VALUES (1,'a'), (2,'b')"])
    code = _mk(["CREATE TABLE t (id INTEGER, kind TEXT "
                "CHECK(kind IN ('a','b','c')))"])
    d = sda.compare_table(prod, code, "t")
    assert d["check_only_in_code"], "代码独有 CHECK 没抓到"
    assert list(d["check_violations"].values()) == [0], "数据全满足, 违反数应为 0"
    sev, why = sda.severity("t", d, 2)
    assert sev == "P3", f"数据全满足却判 {sev} —— 过度告警"
    assert "全部满足" in why


def test_check_violated_by_data_is_p1_with_row_count():
    """数据**真违反**才是 P1, 且必须报出会被拒的行数 (可核验)。"""
    prod = _mk(["CREATE TABLE t (id INTEGER, kind TEXT)",
                "INSERT INTO t VALUES (1,'a'), (2,'zzz'), (3,'qqq')"])
    code = _mk(["CREATE TABLE t (id INTEGER, kind TEXT CHECK(kind IN ('a','b')))"])
    d = sda.compare_table(prod, code, "t")
    assert list(d["check_violations"].values()) == [2]
    sev, why = sda.severity("t", d, 3)
    assert sev == "P1" and "2 行" in why


def test_unrunnable_check_is_p2_never_a_pass():
    """**探针跑不动 != 通过。**

    这条守卫本轮真的挡下了一次假绿: CHECK 抽取的非贪婪正则在 `IN ('a','b')`
    的内层括号处截断, 表达式括号不配平、跑不动。工具当时印的是
    「跑不动(未验)」而不是「通过」—— 否则我会拿着一份空转的绿报告交差。
    """
    prod = _mk(["CREATE TABLE t (id INTEGER)"])
    assert sda.check_violations(prod, "t", "no_such_column > 0") == -1, \
        "跑不动必须返回 -1 (哨兵), 不能是 0"
    d = {"missing_in_code": [], "missing_in_prod": [], "type_drift": [],
         "notnull_drift": [], "default_drift": [], "pk_drift": [],
         "check_only_in_code": ["no_such_column > 0"], "check_only_in_prod": [],
         "check_violations": {"no_such_column > 0": -1}}
    sev, why = sda.severity("t", d, 1)
    assert sev == "P2", f"跑不动被判成 {sev}"
    assert "不是通过" in why, "措辞必须显式否定「通过」, 读的人不会去猜"


# ------------------------------------------------- 抽取器本身 (两次踩坑的回归锚)

def test_extract_checks_survives_nested_parens():
    """`IN (...)` 的内层括号不许截断表达式 —— 非贪婪正则的病根。"""
    got = sda._extract_checks(
        "CREATE TABLE t (k TEXT CHECK(k IN ('a','b','c')), n INT CHECK(n > (1+2)))")
    assert got == ["k IN ('a','b','c')", "n > (1+2)"], got


def test_checks_strip_comments_before_collapsing_whitespace():
    """**先剥 `--` 注释再折叠空白** —— 顺序颠倒 = 空转恒真。

    折叠换行后 `--` 会把其后**整条表达式**吞掉, 剩下的半截拿去当 WHERE 跑,
    永远零违反。AUD-028 的白名单里就带着大段 `--` 注释, 正好踩这个。
    """
    con = _mk(["""CREATE TABLE t (
        kind TEXT CHECK(kind IN (
            'a',   -- 老口径
            'b'    -- AUD-028 补
        ))
    )"""])
    (expr,) = sda.checks_of(con, "t")
    assert "--" not in expr, f"注释没剥干净: {expr}"
    assert "'a'" in expr and "'b'" in expr, f"表达式被注释吞掉了: {expr}"
    assert sda.check_violations(con, "t", expr) == 0, "剥完必须还能真跑"


# ------------------------------------------------- 空转下限

def test_vacuity_guard_rejects_empty_collection(tmp_path, capsys):
    """收集不到 DDL 时必须**判空转并非零退出**, 不能印「零漂移」。

    「标记一切」和「什么都没标记」是同一种病 —— 后者更危险, 因为它长得像好消息。
    """
    db = tmp_path / "e.db"
    sqlite3.connect(db).execute("CREATE TABLE x (i INTEGER)")
    empty_src = tmp_path / "src"
    empty_src.mkdir()
    rc = sda.main(["--db", str(db), "--src", str(empty_src)])
    out = capsys.readouterr().out
    assert rc == 2, "空收集必须非零退出"
    assert "空转判定" in out
    # 判据必须锚**结论行的独有形状**, 不能锚会在反面语境里复现的词。
    # 这处我连错两次: 先锚「零漂移」(工具解释文里就有"此时「零漂移」是空转恒真"),
    # 再锚「对拍」(空转消息自己写着"对拍面 0<10")。两次都是在测措辞而不是测行为。
    # 收尾结论行的真形状是 `对拍 N 表: 一致 M / ...` —— 锚它。
    assert "表: 一致" not in out, "空转时不许再印对拍结论行"


def test_vacuity_thresholds_are_meaningful():
    """下限本身不能被悄悄调成 0 —— 那等于拆掉守卫。"""
    assert sda.MIN_DDL >= 10 and sda.MIN_COMPARED >= 5


# ============================================================ 第二层: 生产实况

def _prod_db():
    for p in PROD_CANDIDATES:
        if p.exists():
            return p
    return None


def test_production_has_no_blocking_drift():
    """对真生产库断言 **P1 == 0 且 P2 == 0** —— 件1 结论的防复发锚。

    有人给 `src` 加一条生产数据不满足的 CHECK, 或删掉生产在用的列, 这条会红。
    """
    db = _prod_db()
    if db is None:
        pytest.skip(f"生产库不在本机 (查过 {[str(p) for p in PROD_CANDIDATES]}) "
                    f"—— 本条只在设备上生效, **此处跳过不代表通过**")
    src_root = db.parent.parent / "src"
    if not src_root.is_dir():
        pytest.skip(f"src 树不在 {src_root} —— 同上, 跳过不代表通过")

    ddls, _ = sda.collect_ddl(src_root)
    code, _ = sda.build_code_db(ddls)
    prod = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        both = sorted(sda.table_names(prod) & sda.table_names(code))
        # 空转守卫: 对拍面塌了的话, 下面的「零 P1」是恒真而非结论
        assert len(ddls) >= sda.MIN_DDL, f"只收集到 {len(ddls)} 个 DDL, 机制失效"
        assert len(both) >= sda.MIN_COMPARED, f"对拍面只剩 {len(both)} 表, 机制失效"

        bad = []
        for t in both:
            d = sda.compare_table(prod, code, t)
            if not any(d.values()):
                continue
            rows = prod.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            sev, why = sda.severity(t, d, rows)
            if sev in ("P1", "P2"):
                bad.append(f"[{sev}] {t}({rows}行): {why}")
        assert not bad, "生产 schema 出现阻断级漂移:\n  " + "\n  ".join(bad)
    finally:
        prod.close()
        code.close()


def test_prod_guard_skip_is_visible_not_silent():
    """**跳过必须是显式状态, 不是沉默。**

    静默跳过正是本轮审计一路在治的病 (`except: pass` / `2>/dev/null` /
    hasattr 探测)。守卫自己长这个病, 就没资格审别人。
    这条断言保证: 生产库缺席时, 上面那条走的是 `pytest.skip` 且带原因,
    而不是「找不到就当通过」。
    """
    import inspect
    src = inspect.getsource(test_production_has_no_blocking_drift)
    assert "pytest.skip" in src, "缺席路径必须显式 skip"
    assert "不代表通过" in src, "skip 原因必须写明「跳过 != 通过」"
    assert PROD_CANDIDATES, "候选路径清单为空 = 这条守卫永远跳过"
