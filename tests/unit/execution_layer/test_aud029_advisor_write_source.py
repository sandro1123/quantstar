# -*- coding: utf-8 -*-
"""AUD-029 RED: advisor 三处写入用 source='advisor', 违反 action_records 的 source 约束。

取证 (2026-09-14, 生产 DDL 原样建表实打):
    action_type=advisory_state  source=advisor  -> 炸 CHECK constraint failed:
        source IN ('strategy', 'agent', 'execution', 'manual')
    pool_downgrade / pool_exit  同上
    status_change               source=execution -> 成功
旁证: 生产 action_records 的 source 实际只有 execution 1576 / strategy 529 /
manual 251 三值, **advisor 恰好 0 行** —— 这三条路从来没写成功过。

后果 (为什么急):
  `intraday_scan.py:48` 的 try/except 兜底让主扫描不受影响 (设计对的),
  代价是 advisor 段**整段零写**只留一行 warning:
    - A 席盘中域红线「advisory_state 0 行留痕 = 挂线失效」-> 明日首火必判 0 行
    - D 席 ex4 的池迁移数同样恒 0
  两席都会把「零写」误诊成自己的接线没通电。真因是这条 CHECK。

修法: source 那四个值就是**层名 + manual**, advisor 属执行层 -> 写 'execution'。
「是 advisor 干的」已由 action_type (advisory_state/pool_downgrade/pool_exit)
承载, 零信息损失; 反方案「把 advisor 加进 CHECK」需重建生产表, 为 3 行不值。

顺带同批: 留痕与状态迁移的**半提交面** —— 见 test_*_leaves_no_half_commit。
"""
import re
import sqlite3
from pathlib import Path

import pytest
from common.database import Database
from execution_layer import ExecutionLayer

SRC = Path(__file__).resolve().parents[3] / "src"


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _source_vocab(ddl: str) -> set[str]:
    """从 DDL 抽 source 词表。逐个取**引号内**的词 —— 早先用
    replace("'", " ").split() 把逗号也当成了词, 被 vacuous 守卫当场抓住。
    """
    inner = re.search(
        r"source\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*source\s+IN\s*\(([^)]*)\)",
        ddl).group(1)
    return set(re.findall(r"'([a-z_]+)'", inner))


def _records(conn, action_type):
    return conn.execute(
        "SELECT COUNT(*) FROM action_records WHERE action_type=?",
        (action_type,)).fetchone()[0]


# ---------------------------------------------------------------- 三条写入面

def test_pool_downgrade_write_succeeds(exec_layer):
    """B1 推荐池到期降级: 状态要迁移, 留痕要落库。"""
    from execution_layer.advisor import recommendation_advisor as ra
    pool = exec_layer.stock_pool
    pool.add("000001", "平安银行")
    conn = pool.db.get_connection()
    conn.execute("UPDATE stock_pool SET status='pending_entry', valid_until='2026-09-01' "
                 "WHERE code='000001'")
    conn.commit()

    moved = ra.pool_maintenance(pool.db, today="2026-09-14")

    assert moved >= 1
    assert pool.get("000001")["status"] == "watch"
    assert _records(conn, "pool_downgrade") == 1, "降级零留痕"


def test_pool_exit_write_succeeds(exec_layer):
    """B2 跟踪池跌破观察期低点退出: 同样要留痕。"""
    from execution_layer.advisor import recommendation_advisor as ra
    pool = exec_layer.stock_pool
    pool.add("000002", "万科A")
    conn = pool.db.get_connection()
    # entry_low=10, last_price=8 -> 跌破 8% 线 (默认 obs_low_drop_pct=8.0)
    conn.execute("UPDATE stock_pool SET status='watch', entry_low=10.0, last_price=8.0 "
                 "WHERE code='000002'")
    conn.commit()

    ra.pool_maintenance(pool.db, today="2026-09-14")

    assert pool.get("000002")["status"] == "exited"
    assert _records(conn, "pool_exit") == 1, "跟踪池退出零留痕"


def test_ex4_advisory_state_ledger_is_not_empty(exec_layer):
    """A 席红线正面: 收盘轮每候选一行 advisory_state 留痕。

    这条就是明日 15:00 首火的头号判据 —— 此前它恒为 0, 真因在这里。
    """
    from execution_layer.scripts.ex4_cli_scan import advisor_segment
    pool = exec_layer.stock_pool
    for code, name in (("000001", "平安银行"), ("000002", "万科A")):
        pool.add(code, name)
    conn = pool.db.get_connection()
    conn.execute("UPDATE stock_pool SET last_price=10.0")
    conn.commit()

    advisor_segment(pool.db, today="2026-09-14", now_hhmm="15:30")

    assert _records(conn, "advisory_state") == 2, "三态清单零留痕 (A 席红线)"


# ---------------------------------------------------------------- 半提交面

def test_downgrade_failure_leaves_no_half_commit(exec_layer, monkeypatch):
    """留痕写不进去时, **状态不许已经改掉** —— 否则就是「改了没留痕」。

    病根: 旧实现先 UPDATE 后 INSERT。_LockSafeConnection 只回滚
    「失败语句自己开的事务」, UPDATE 已先开了事务 -> INSERT 失败时
    was_in_tx=True 不回滚, 那条 UPDATE 悬在未提交事务里等别人 commit。
    """
    from execution_layer.advisor import recommendation_advisor as ra
    pool = exec_layer.stock_pool
    pool.add("000003", "测试股")
    conn = pool.db.get_connection()
    conn.execute("UPDATE stock_pool SET status='pending_entry', valid_until='2026-09-01' "
                 "WHERE code='000003'")
    conn.commit()

    real_execute = conn.execute

    def boom(sql, params=()):
        if "INSERT INTO action_records" in sql:
            raise sqlite3.IntegrityError("probe: 留痕写入失败")
        return real_execute(sql, params)

    monkeypatch.setattr(conn, "execute", boom)
    with pytest.raises(sqlite3.IntegrityError):
        ra.pool_maintenance(pool.db, today="2026-09-14")
    monkeypatch.undo()

    assert pool.get("000003")["status"] == "pending_entry", (
        "留痕失败却把状态改了 —— 半提交 (改了没留痕)")


# ---------------------------------------------------------------- 机器断言 (防复发)

def test_no_src_write_uses_source_outside_schema():
    """守卫: src 里写 action_records 的 source 字面量必须在 DDL 词表内。

    与 AUD-028 的 action_type 守卫同族 —— 把「记得用合法值」变成机器断言,
    而不是一句叮嘱。本次审计最贵的一课。
    """
    from execution_layer.tables import ACTION_RECORDS_DDL
    vocab = _source_vocab(ACTION_RECORDS_DDL)

    ins = re.compile(
        r"INSERT\s+INTO\s+action_records\s*\(([^)]*)\)[\s\S]{0,200}?VALUES\s*\(([^)]*)\)",
        re.I)
    bad = {}
    for p in sorted(SRC.rglob("*.py")):
        for cols_s, vals_s in ins.findall(p.read_text(encoding="utf-8")):
            cols = [c.strip().strip('"\'') for c in cols_s.split(",")]
            vals = [v.strip() for v in vals_s.split(",")]
            if "source" not in cols or len(cols) != len(vals):
                continue
            v = vals[cols.index("source")]
            if v.startswith(("'", '"')) and v.strip("'\"") not in vocab:
                bad[str(p.relative_to(SRC))] = v.strip("'\"")
    assert bad == {}, f"source 取值不在 DDL 词表 {sorted(vocab)} 内, 写入必被 CHECK 拒: {bad}"


def test_source_vocab_guard_is_not_vacuous():
    """守卫的守卫: 词表必须真解析出四个值, 否则上一条空转恒绿。"""
    from execution_layer.tables import ACTION_RECORDS_DDL
    vocab = _source_vocab(ACTION_RECORDS_DDL)
    assert vocab == {"strategy", "agent", "execution", "manual"}, vocab
