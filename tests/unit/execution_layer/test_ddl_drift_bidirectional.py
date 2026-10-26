# -*- coding: utf-8 -*-
"""DDL 漂移守卫双向化 (2026-09-25, 统筹裁定形态)。

病 (修法丙施工中暴露): 既有对拍守卫 `test_v10_*_ddl_matches_authoritative`
是**单向**的 —— 只断言「迁移清单 ⊇ 权威 DDL」(注释明写"清单可多增量列"),
于是「**权威 DDL 少列**」这个方向从来没人看。

实爆: TRACKING_DDL 缺 confidence_tier/confidence_reason/entry_basis_trace_id
三列 (它们只在 v10 迁移里), 生产因 v10 先跑而无恙, 但懒建路径建出的表缺列
—— tier/reason 一下沉进 start() 就当场炸 (修法丙 RED 首轮的 OperationalError)。

判例「单向漂移守卫只挡一个方向: 允许 A⊇B 的对拍, 挡不住 B 缺列」。

统筹裁定的双向形态:
  正向 (保留): 迁移清单 ⊇ 权威 DDL 的业务列  — v10 增量列靠它保护, 不能丢
  反向 (新增): 权威 DDL ⊇ 迁移清单的业务列  — 权威不得缺列
  出口: **增量列豁免白名单** MIGRATION_ONLY_COLUMNS —— 仅迁移期存在的过渡列
        允许权威没有, 但必须**显式登记理由**(白名单即文档, 非静默豁免)
"""
import re

import pytest

# ── 增量列豁免白名单 ───────────────────────────────────────────────
# {(表名, 列名): "为什么这列只在迁移清单里、权威 DDL 可以没有"}
# 空 = 当前无合法差异。加条目必须写理由 —— 由 test_exemptions_are_documented 强制。
MIGRATION_ONLY_COLUMNS: dict[tuple[str, str], str] = {}


def _normalize(ddl: str) -> str:
    ddl = re.sub(r"--[^\n]*", "", ddl)
    return re.sub(r"\s+", " ", ddl).strip().rstrip(";").strip()


def _columns_of(ddl: str) -> set[str]:
    """从 CREATE TABLE 字面量里抽业务列名 (跳过约束行)。"""
    body = _normalize(ddl)
    m = re.search(r"\((.*)\)\s*$", body, re.S)
    if not m:
        return set()
    cols = set()
    depth = 0
    cur = ""
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.add(cur.strip())
            cur = ""
        else:
            cur += ch
    cols.add(cur.strip())
    out = set()
    for c in cols:
        c = c.strip()
        if not c:
            continue
        head = c.split()[0].upper()
        if head in ("UNIQUE", "PRIMARY", "FOREIGN", "CHECK", "CONSTRAINT"):
            continue
        out.add(c.split()[0])
    return out


def _migration_ddl_for(table: str) -> str:
    from execution_layer.migrations import MIGRATIONS
    v10 = dict(MIGRATIONS)[10]
    for stmt in v10:
        if f"CREATE TABLE IF NOT EXISTS {table}" in stmt:
            return stmt
    raise AssertionError(f"v10 清单无 {table} 建表语句")


def _authoritative_ddl(table: str) -> str:
    from execution_layer.tracking import recommendation_tracker as RT
    mapping = {
        "recommendation_tracking": RT.TRACKING_DDL,
        "recommendation_tracking_advice": RT.ADVICE_DDL,
    }
    if table not in mapping:
        raise AssertionError(f"未登记权威 DDL: {table}")
    return mapping[table]


TABLES = ["recommendation_tracking", "recommendation_tracking_advice"]


@pytest.mark.parametrize("table", TABLES)
def test_migration_superset_of_authoritative(table):
    """正向 (保留既有语义): 迁移清单不得缺权威 DDL 的任何业务列。"""
    mig_cols = _columns_of(_migration_ddl_for(table))
    auth_cols = _columns_of(_authoritative_ddl(table))
    missing = auth_cols - mig_cols
    assert not missing, (
        f"{table}: 迁移清单缺权威列 {sorted(missing)} —— 迁移建出的表会少列"
    )


@pytest.mark.parametrize("table", TABLES)
def test_authoritative_superset_of_migration(table):
    """★反向 (新增): 权威 DDL 不得缺迁移清单的业务列。

    治「懒建路径建出的表缺列」—— 生产靠迁移先跑而无恙, 但任何走
    `CREATE TABLE IF NOT EXISTS <权威DDL>` 的路径 (测试 / 新库 / 懒建)
    都会拿到一张少列的表, 写那些列时当场炸。

    合法差异走 MIGRATION_ONLY_COLUMNS 白名单, 且必须写明理由。
    """
    mig_cols = _columns_of(_migration_ddl_for(table))
    auth_cols = _columns_of(_authoritative_ddl(table))
    missing = mig_cols - auth_cols
    unexcused = {c for c in missing if (table, c) not in MIGRATION_ONLY_COLUMNS}
    assert not unexcused, (
        f"{table}: 权威 DDL 缺迁移清单的列 {sorted(unexcused)} —— 懒建路径会少列。"
        f"若确属「仅迁移期过渡列」, 请登记进 MIGRATION_ONLY_COLUMNS 并写明理由"
    )


def test_exemptions_are_documented():
    """白名单即文档: 每条豁免必须有非空理由, 不得静默豁免。"""
    for key, reason in MIGRATION_ONLY_COLUMNS.items():
        assert isinstance(key, tuple) and len(key) == 2, (
            f"白名单键须为 (表名, 列名), 实际 {key!r}"
        )
        assert reason and len(str(reason).strip()) >= 10, (
            f"豁免 {key} 的理由过短或为空: {reason!r} —— 白名单是文档不是黑洞"
        )


def test_tracking_increment_columns_now_in_authoritative():
    """回归锚: v10 三增量列已收编进权威 DDL (修法丙前置修), 不得退回。"""
    auth_cols = _columns_of(_authoritative_ddl("recommendation_tracking"))
    for c in ("confidence_tier", "confidence_reason", "entry_basis_trace_id"):
        assert c in auth_cols, (
            f"权威 TRACKING_DDL 退回缺 {c} —— 懒建表会缺列, start() 写它时炸"
        )
