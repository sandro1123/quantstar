# -*- coding: utf-8 -*-
"""件1 · 全表 DDL-vs-生产 schema 对拍（只读）。

## 为什么对拍的是「按代码真建出来的库」而不是「源码文本」
恢复演练若走「重建 + 回灌」，真正会发生的是：**执行代码里的 DDL 建出一个新库，
再把生产数据灌进去**。所以要对拍的两个对象是
    ① 按代码 DDL 真建出来的 schema   ② 生产库现有的 schema
比源码文本更贴近那个场景 —— 文本比对会漏掉「拼接/条件建表」的真实结果。

## 判据来源（AUD-028 实证）
`action_records` 的代码 DDL 带 26 项 `action_type` CHECK，生产表**根本没有**该约束，
且生产在册 4 种类型不在白名单 —— **按代码 DDL 重建再回灌，那 82 行会被整批拒收**。
AUD-028 只查了这一张；**其余 88 张有没有同族漂移，至今无人查过**。这是死线前
唯一没人碰过的数据安全面盲区，也是 AUD-024 判「能不能走重建+回灌」的前置。

## 本脚本自带的纪律（都是我自己踩出来的判例）
- **首行打印「我在验什么」**：基座指纹（库路径/表数/commit/收集到的 DDL 数）——
  上次 `git checkout main 2>/dev/null` 失败被吞、我差点把自己分支当 main 报，
  救我的就是这一行。
- **逐项 try 隔离**：一处失败不带走其余；**探针错与发现显式分开印**
  （否则「没打印」会被读成「没问题」）。
- **守卫的守卫**：收集到的 DDL 数与对拍表数必须过下限，否则判空转并非零退出 ——
  「标记一切」和「什么都没标记」是同一种病。
- **正反措辞分开**：差异面用「漂移/一致」，缺失面用「仅生产有/仅代码有」，不混用。
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import sqlite3
import sys
from pathlib import Path

# 空转下限：低于此数说明收集/对拍机制本身坏了，不是"没问题"
MIN_DDL = 15
MIN_COMPARED = 10


# ----------------------------------------------------------------- 代码侧

def collect_ddl(src_root: Path) -> tuple[dict[str, str], list[str]]:
    """扫 src 全树，取**模块级字符串常量**里含 CREATE TABLE 的 DDL。

    用 AST 而不是正则：正则会把函数内的、注释里的、f-string 拼一半的都捞进来。
    返回 ({模块:常量名 -> DDL 文本}, [探针错])
    """
    out, errs = {}, []
    for p in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError as e:
            errs.append(f"{p.relative_to(src_root)}: 解析失败 {e}")
            continue
        for node in tree.body:                      # 只认模块级
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            text = node.value.value
            if "CREATE TABLE" not in text.upper():
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[f"{p.relative_to(src_root)}::{t.id}"] = text
    return out, errs


def build_code_db(ddls: dict[str, str]) -> tuple[sqlite3.Connection, list[str]]:
    """把所有 DDL 执行进一个空库 —— 这就是「按代码重建」的真实产物。"""
    con = sqlite3.connect(":memory:")
    errs = []
    for name, text in ddls.items():
        try:
            con.executescript(text)
        except sqlite3.Error as e:
            errs.append(f"{name}: {str(e)[:70]}")
    con.commit()
    return con, errs


# ----------------------------------------------------------------- 对拍

def table_names(con) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


def cols_of(con, t) -> dict[str, dict]:
    return {r[1]: {"type": (r[2] or "").upper(), "notnull": r[3],
                   "default": r[4], "pk": r[5]}
            for r in con.execute(f"PRAGMA table_info({t})")}


def _extract_checks(sql: str) -> list[str]:
    """按**括号配平**抽 `CHECK(...)` 的表达式。

    首版用非贪婪正则 `CHECK\\s*\\((.*?)\\)`，在 `IN ('a','b')` 这种**内层括号**处
    就截断了，抽出来的表达式括号不配平、拿去跑必报语法错。
    当时工具印的是「跑不动(未验)」而**不是**「通过」—— 那条
    「探针错 != 通过」的守卫挡住了一次假绿，否则我会拿着一份空转的绿报告交差。
    """
    out, i, up = [], 0, sql.upper()
    while True:
        j = up.find("CHECK", i)
        if j < 0:
            return out
        k = sql.find("(", j)
        if k < 0:
            return out
        depth, m = 0, k
        while m < len(sql):
            if sql[m] == "(":
                depth += 1
            elif sql[m] == ")":
                depth -= 1
                if depth == 0:
                    break
            m += 1
        if depth != 0:                     # 配不平: 宁可漏也不给半条
            return out
        out.append(sql[k + 1:m])
        i = m + 1


def checks_of(con, t) -> set[str]:
    """抽该表 DDL 里的 CHECK 子句。

    **先剥 `--` 行注释再归一化空白** —— 顺序颠倒会出事: 折叠换行后 `--` 会把
    其后**整条表达式**吞掉, 拿去当 WHERE 跑就永远是"零违反"(空转恒真)。
    AUD-028 的白名单里就带着大段 `--` 注释, 正好踩这个。
    """
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
    if not row or not row[0]:
        return set()
    out = set()
    for m in _extract_checks(row[0]):
        lines = [re.sub(r"--.*$", "", ln) for ln in m.splitlines()]
        out.add(re.sub(r"\s+", " ", " ".join(lines)).strip().lower())
    return out


def check_violations(prod, t: str, expr: str) -> int:
    """**有约束 != 会拒数据**: 拿生产真数据实打该 CHECK, 数被拒的行。

    第一版把「代码独有 CHECK」直接判 P1 —— 过度告警。实打后 5 条全过:
    `action_records` 那条正是 AUD-028 扩到 33 项修好的, 生产数据全满足。
    把"有约束"当成"会拒数据"报出去, 就是我一路在治的「判据面 != 事实面」。
    返回 -1 = 表达式跑不了(探针错), 不等于零违反。
    """
    try:
        return prod.execute(f"SELECT COUNT(*) FROM {t} WHERE NOT ({expr})").fetchone()[0]
    except sqlite3.Error:
        return -1


def compare_table(prod, code, t) -> dict:
    pc, cc = cols_of(prod, t), cols_of(code, t)
    d = {"missing_in_code": sorted(set(pc) - set(cc)),
         "missing_in_prod": sorted(set(cc) - set(pc)),
         "type_drift": [], "notnull_drift": [], "default_drift": [],
         "pk_drift": [], "check_only_in_code": [], "check_only_in_prod": []}
    for c in sorted(set(pc) & set(cc)):
        a, b = pc[c], cc[c]
        if a["type"] != b["type"]:
            d["type_drift"].append(f"{c}: 生产={a['type'] or '<无>'} 代码={b['type'] or '<无>'}")
        if a["notnull"] != b["notnull"]:
            d["notnull_drift"].append(f"{c}: 生产NOTNULL={a['notnull']} 代码={b['notnull']}")
        if (a["default"] or "") != (b["default"] or ""):
            d["default_drift"].append(f"{c}: 生产={a['default']} 代码={b['default']}")
        if a["pk"] != b["pk"]:
            d["pk_drift"].append(f"{c}: 生产PK={a['pk']} 代码={b['pk']}")
    pch, cch = checks_of(prod, t), checks_of(code, t)
    d["check_only_in_code"] = sorted(cch - pch)
    d["check_only_in_prod"] = sorted(pch - cch)
    # 实打: 代码独有的每条 CHECK, 生产数据到底违不违反
    d["check_violations"] = {e: check_violations(prod, t, e)
                             for e in d["check_only_in_code"]}
    return d


# 分级：哪些差异会让「重建 + 回灌」真的失败
def severity(t: str, d: dict, rows: int) -> tuple[str, str]:
    # 代码独有 CHECK: 只有**实打确认真会拒数据**才判 P1 (见 check_violations)
    v = d.get("check_violations", {})
    real = {e: n for e, n in v.items() if n > 0}
    probe_err = [e for e, n in v.items() if n < 0]
    if real:
        return ("P1", f"代码独有 CHECK **实打会拒 {sum(real.values())} 行** "
                      f"(该表 {rows} 行) —— 回灌必败 (AUD-028 同族)")
    if probe_err:
        return ("P2", f"代码独有 CHECK {len(probe_err)} 条**跑不动**(探针错) —— "
                      f"未验, 不是通过")
    if d["check_only_in_code"]:
        return ("P3", f"代码独有 CHECK {len(d['check_only_in_code'])} 条, "
                      f"但生产数据**实打全部满足** —— 不阻断回灌")
    if d["missing_in_code"]:
        return ("P1", f"生产有而代码不建的列 {d['missing_in_code']} —— 回灌必报 no such column")
    if d["notnull_drift"] or d["pk_drift"]:
        return ("P2", "NOT NULL / 主键 口径不一致 —— 回灌可能被约束拒")
    if d["type_drift"]:
        return ("P3", "列类型声明不一致 (SQLite 亲和性通常兜得住, 但语义已漂移)")
    if d["missing_in_prod"] or d["default_drift"] or d["check_only_in_prod"]:
        return ("P3", "代码多列 / 默认值 / 生产独有 CHECK —— 不阻断回灌")
    return ("", "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全表 DDL-vs-生产 schema 对拍 (只读)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--src", required=True)
    a = ap.parse_args(argv)

    src_root = Path(a.src)
    prod = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    ddls, scan_errs = collect_ddl(src_root)
    code, build_errs = build_code_db(ddls)

    p_tabs, c_tabs = table_names(prod), table_names(code)
    both = sorted(p_tabs & c_tabs)

    # ---- 首行：我在验什么（基座指纹；上次没读它差点报错结论）
    print("=" * 74)
    print("全表 DDL-vs-生产 schema 对拍")
    print(f"  生产库      : {a.db}   ({len(p_tabs)} 表)")
    print(f"  代码树      : {src_root}")
    print(f"  收集 DDL    : {len(ddls)} 个模块级常量 -> 建出 {len(c_tabs)} 表")
    print(f"  两侧都有    : {len(both)} 表 (本次真对拍面)")
    print("=" * 74)

    # ---- 守卫的守卫：低于下限说明机制坏了，不是"没问题"
    if len(ddls) < MIN_DDL or len(both) < MIN_COMPARED:
        print(f"\n**空转判定**: DDL {len(ddls)}<{MIN_DDL} 或对拍面 {len(both)}<{MIN_COMPARED}")
        print("收集/建库机制本身失效 —— 此时「零漂移」是空转恒真, 不是结论。")
        return 2

    if scan_errs:
        print(f"\n[探针错, 非发现] 源码解析失败 {len(scan_errs)} 处:")
        for e in scan_errs[:5]:
            print(f"    {e}")
    if build_errs:
        print(f"\n[探针错, 非发现] DDL 执行失败 {len(build_errs)} 处:")
        for e in build_errs[:8]:
            print(f"    {e}")

    # ---- 面①：表存在性
    only_prod = sorted(p_tabs - c_tabs)
    only_code = sorted(c_tabs - p_tabs)
    print(f"\n--- 面① 表存在性 ---")
    print(f"  仅生产有 (代码无模块级 DDL, 靠函数内建表/迁移/历史遗留): {len(only_prod)} 张")
    print(f"    {only_prod[:18]}{' …' if len(only_prod) > 18 else ''}")
    print(f"  仅代码有 (生产尚未建): {len(only_code)} 张  {only_code[:10]}")
    print("  注: 「仅生产有」**不等于**有病 —— 它只说明该表不由模块级 DDL 建。")
    print("      但它确实意味着: **按模块级 DDL 重建, 这些表不会出现**。")

    # ---- 面②③④：列/类型/约束
    print(f"\n--- 面②③④ 列 / 类型 / 约束 (逐表, 仅印有差异者) ---")
    findings, clean = [], 0
    for t in both:
        try:
            d = compare_table(prod, code, t)
        except Exception as e:                                 # 逐项隔离
            print(f"  [探针错, 非发现] {t}: {type(e).__name__} {str(e)[:60]}")
            continue
        if not any(d.values()):
            clean += 1
            continue
        try:
            rows = prod.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            rows = -1
        sev, why = severity(t, d, rows)
        findings.append((sev, t, rows, d, why))

    order = {"P1": 0, "P2": 1, "P3": 2, "": 3}
    findings.sort(key=lambda x: (order[x[0]], -x[2]))
    for sev, t, rows, d, why in findings:
        print(f"\n  [{sev or '--'}] {t}  ({rows} 行)")
        if why:
            print(f"        {why}")
        for k, label in (("missing_in_code", "生产有·代码不建的列"),
                         ("missing_in_prod", "代码有·生产无的列"),
                         ("type_drift", "类型漂移"),
                         ("notnull_drift", "NOT NULL 漂移"),
                         ("pk_drift", "主键漂移"),
                         ("default_drift", "默认值漂移"),
                         ("check_only_in_code", "代码独有 CHECK"),
                         ("check_only_in_prod", "生产独有 CHECK")):
            v = d[k]
            if not v:
                continue
            if k == "check_only_in_code":
                for e in v:
                    n = d["check_violations"].get(e, -1)
                    verdict = ("实打 **拒 %d 行**" % n if n > 0
                               else "实打全过" if n == 0 else "**跑不动(未验)**")
                    print(f"        代码独有 CHECK -> {verdict}: {e[:90]}")
                continue
            print(f"        {label}: {v[:4]}{' …' if len(v) > 4 else ''}")

    # ---- 收尾账（计划 vs 实跑，缺席自己显形）
    print("\n" + "=" * 74)
    n1 = sum(1 for f in findings if f[0] == "P1")
    n2 = sum(1 for f in findings if f[0] == "P2")
    n3 = sum(1 for f in findings if f[0] == "P3")
    print(f"对拍 {len(both)} 表: 一致 {clean} / 有差异 {len(findings)} "
          f"(P1 {n1} · P2 {n2} · P3 {n3})")
    print(f"表存在性: 仅生产 {len(only_prod)} / 仅代码 {len(only_code)}")
    print(f"探针错: 源码解析 {len(scan_errs)} · DDL 执行 {len(build_errs)} "
          f"(这些是**工具的**失败, 不是被测对象的性质)")
    print("=" * 74)
    prod.close(); code.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
