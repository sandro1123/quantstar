# -*- coding: utf-8 -*-
"""AUD-028 RED: action_records 的 action_type 白名单已跨代漂移。

取证 (2026-09-14, 生产库只读 + 生产 DDL 原样建表实打):
  - 生产表 **根本没有** action_type CHECK (只有 source CHECK);
    代码 DDL `tables.py:79` 却带着一份 26 项白名单 —— 两者从来不是同一张表。
  - src 现有写点里 3 种不在白名单: advisory_state / pool_downgrade / pool_exit
    -> 任何按代码 DDL 新建的库 (测试/回测/paper trading/**灾难恢复重建**) 上,
       这三条写入必抛 IntegrityError。
  - 生产在册 4 种历史类型也不在白名单:
       buy_pending_review(48) / sell_pending_review(30)
       / buy_rejected(2) / stale_pending_cleanup(2)
    -> **按代码 DDL 重建 action_records 再回灌生产数据, 这 82 行会被拒收**
       (直接命中 AUD-024「恢复路径未演练」)。

修法不是「记得登记」而是**机器断言**: 本文件第一条测试扫 src 全树写入点,
不在白名单就红。这是本次审计最贵的一课 —— 判例写下来不等于内化,
真正管用的是把它做成可执行的断言。
"""
import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src"

# 生产在册但当前 src 已无写点的历史类型 (2026-09-14 生产普查, 括号内为行数)
LEGACY_PRODUCTION_TYPES = {
    "buy_pending_review": 48,
    "sell_pending_review": 30,
    "buy_rejected": 2,
    "stale_pending_cleanup": 2,
}


def _whitelist() -> set[str]:
    from execution_layer.tables import ACTION_RECORDS_DDL
    m = re.search(r"action_type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*action_type\s+IN\s*\(([\s\S]*?)\)\s*\)",
                  ACTION_RECORDS_DDL)
    assert m, "DDL 里找不到 action_type CHECK —— 约束被整条删掉了?"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def _written_types() -> dict[str, str]:
    """普查 src 全树**真正写进 action_records** 的 action_type 字面量 -> 出处。

    两条写法都要认, 且都必须精确 —— 宽正则会把别的 `.write()` 当成命中
    (memory_layer 的 feedback/lesson 就是这么误入的), 守卫测试自己假阳
    比没有守卫更坏。
      ① action_record.write(code, "<type>", ...)   -> AST 取第 2 位实参
      ② INSERT INTO action_records (...cols...) VALUES (...)  -> 按列位取值
    """
    found: dict[str, str] = {}
    ins = re.compile(
        r"INSERT\s+INTO\s+action_records\s*\(([^)]*)\)[\s\S]{0,200}?VALUES\s*\(([^)]*)\)",
        re.I)
    for p in sorted(SRC.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        rel = str(p.relative_to(SRC))

        # ① AST: 只认属性链末端是 action_record 的 .write(...)
        try:
            tree = ast.parse(text)
        except SyntaxError:                                   # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write"):
                continue
            recv = node.func.value
            recv_name = recv.attr if isinstance(recv, ast.Attribute) else (
                recv.id if isinstance(recv, ast.Name) else "")
            if recv_name != "action_record":
                continue
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                found.setdefault(node.args[1].value, rel)

        # ② 裸 INSERT: 按列名定位, 不靠"看着像" (否则 source='advisor' 会混进来)
        for cols_s, vals_s in ins.findall(text):
            cols = [c.strip().strip('"\'') for c in cols_s.split(",")]
            vals = [v.strip() for v in vals_s.split(",")]
            if "action_type" not in cols or len(cols) != len(vals):
                continue
            v = vals[cols.index("action_type")]
            if v.startswith(("'", '"')):
                found.setdefault(v.strip("'\""), rel)
    return found


def test_all_src_written_action_types_are_registered():
    """凡 src 里写进 action_records 的 action_type, 必须在 DDL 白名单里。

    这条红 = 有人新增了写点却没登记 -> 新建库上必抛 IntegrityError。
    """
    wl = _whitelist()
    written = _written_types()
    unregistered = {t: src for t, src in written.items() if t not in wl}
    assert unregistered == {}, (
        f"{len(unregistered)} 种 action_type 有写点但未登记进 DDL 白名单 "
        f"(新建库上必炸): {unregistered}")


def test_census_is_not_vacuous():
    """守卫的守卫: 普查必须真扫到东西, 否则上一条是空转恒绿。"""
    written = _written_types()
    assert len(written) >= 15, f"普查只扫到 {len(written)} 种, 正则/AST 失效了"
    assert "status_change" in written
    for false_positive in ("feedback", "lesson", "advisor"):
        assert false_positive not in written, (
            f"{false_positive} 是假阳 —— 它不是 action_type")


def test_legacy_production_types_are_registered():
    """生产在册的历史类型也要登记 —— 否则重建表回灌数据时被拒收。

    这 4 种当前 src 已无写点, 但生产共 82 行在库。AUD-024 的恢复演练
    若走「按 DDL 重建 + 回灌」, 这 82 行会整批被 CHECK 挡掉。
    """
    wl = _whitelist()
    missing = sorted(t for t in LEGACY_PRODUCTION_TYPES if t not in wl)
    assert missing == [], (
        f"生产在册历史类型未登记, 重建表回灌会拒收: {missing} "
        f"(行数 {[LEGACY_PRODUCTION_TYPES[t] for t in missing]})")


def test_whitelist_is_clean():
    wl_raw = re.findall(
        r"'([a-z_]+)'",
        re.search(r"action_type\s+IN\s*\(([\s\S]*?)\)\s*\)",
                  __import__("execution_layer.tables", fromlist=["x"]).ACTION_RECORDS_DDL).group(1))
    assert len(wl_raw) == len(set(wl_raw)), (
        f"白名单有重复项: {sorted({t for t in wl_raw if wl_raw.count(t) > 1})}")


def test_check_constraint_still_enforced():
    """反面: 白名单外的类型必须仍被拒 —— 防有人图省事把 CHECK 整条删了。"""
    import sqlite3
    from execution_layer.tables import ACTION_RECORDS_DDL
    conn = sqlite3.connect(":memory:")
    conn.execute(ACTION_RECORDS_DDL)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO action_records (code, action_type, reason, source) "
            "VALUES ('000001', 'definitely_not_a_real_action_type', 'probe', 'execution')")
