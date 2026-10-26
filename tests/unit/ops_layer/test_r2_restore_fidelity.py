# -*- coding: utf-8 -*-
"""Round2 面A RED — 恢复路径的值保真与判据面覆盖。

背景: 09-14 恢复演练判 PASS (15 表值面全绿)。复审用真 dump/replay/fingerprint
做了一次含四种值形态的往返实验, 结论:

| 用例        | 原值                | 恢复值   | 判定 |
|-------------|---------------------|----------|------|
| 真 NULL     | None                | None     | OK   |
| 文本 'None' | 'None'              | **NULL** | 坏   |
| 前后空白    | '  padded  '        | 'padded' | 坏   |
| 真换行      | 'a\\n  b'            | 保真     | OK   |
| 数值类型    | integer/real        | 保真     | OK   |

两处真损坏, 病根同一族: **用字符串当哨兵**。
  `_split_sql_values` 把 SQL NULL 解成字符串 "None", `replay_increment` 再把
  `v == "None"` 映回 None —— 于是**真实文本值 'None' 也被映成 NULL**。
  同族第二处: 未加引号的 `NULL` 令牌判据是 `v == "NULL"`, 文本值 'NULL' 同样中招。
  同族第三处: `_sql_literal` 只转义换行不转义反斜杠本身, 于是**字面**的
  反斜杠+n 与**真换行**在 dump 里长得一模一样, 还原时前者被误判成换行 ——
  转义函数不是单射, 往返就不可能保真。
  空白那处是 `"".join(buf).strip()` 无条件剥离 —— 它本意是去掉分隔符 `, ` 的空格,
  却连引号**内部**的真实空白一起吃掉。

更要命的是**验证面对第一种损坏恰好是盲的**:
  `_fingerprint` 用 `str(v)`, 而 `str(None) == str('None') == 'None'` ——
  live 的文本 'None' 与 restored 的 NULL 产生完全相同的指纹。
  **损坏与验证同时失效**, 这是最危险的形态 (判例: 验证面必须能看见它要抓的那种病)。

判据面覆盖 (第二组):
  生产 89 表, `INCREMENTAL_TABLES` 只有 15 张, **74 张不在增量面**; 其中
  `stock_pool` / `post_exit_tracking` / `execution_stats` / `model_predictions`
  / `factor_weights` 五张执行·模型核心表全部缺席。而 `verify_restore` 的终判面
  正是这 15 张 —— 「15/15 值面全绿」被读成「恢复能力已验证」时, 范围被高估。
  (AUD-005 那 18 笔回填的宿主表就是 `stock_pool` —— 我上轮据以主张 REPLACE
   语义的那个例子, 它的表根本不在 dump 面。)
  修法不是把 74 张全塞进增量 (那是排期决策, 呈裁), 而是**让缺口可见**:
  与该模块自己已采用的「空表 = 可见的 0 行条目, 不是缺席」同一条纪律。
"""
import os
import sqlite3

import pytest

DDL = ("CREATE TABLE alerts (id INTEGER PRIMARY KEY, created_at TEXT, "
       "level TEXT, detail TEXT, n INTEGER, x REAL)")
DAY = "2026-09-15"


@pytest.fixture
def one_table(monkeypatch, tmp_path):
    """单表往返夹具: 返回 (src_path, dst_path, backup_root)。

    monkeypatch 改模块级清单 —— 不直接赋值, 免得跨测试残留
    (判例: 模块/函数级单例污染必须由夹具还原)。
    """
    import ops_layer.incremental_backup as ib
    monkeypatch.setattr(ib, "INCREMENTAL_TABLES", [("alerts", "created_at", False)])
    monkeypatch.setattr(ib, "FULL_DUMP_TABLES", {"alerts"})
    src = str(tmp_path / "src.db")
    dst = str(tmp_path / "restored.db")
    for p in (src, dst):
        c = sqlite3.connect(p)
        c.execute(DDL)
        c.commit()
        c.close()
    return src, dst, str(tmp_path / "bk")


def _roundtrip(one_table, rows):
    """写入 rows → dump → replay 到空库 → 返回 (src_con, dst_con)。"""
    import ops_layer.incremental_backup as ib
    src, dst, bk = one_table
    c = sqlite3.connect(src)
    c.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    out = ib.run_incremental_backup(src, bk, date=DAY)
    ib.replay_increment(out["dir"], dst)
    return sqlite3.connect(src), sqlite3.connect(dst)


def _detail(con, rid):
    return con.execute("SELECT detail FROM alerts WHERE id=?", (rid,)).fetchone()[0]


# ------------------------------------------------------------ 值保真 (损坏面)

@pytest.mark.parametrize("rid,value,label", [
    (1, "None", "文本 'None' 被哨兵吃成 NULL"),
    (2, "NULL", "文本 'NULL' 被哨兵吃成 NULL (同族第二处)"),
    (3, "  padded  ", "引号内前后空白被 .strip() 吃掉"),
    (4, " ", "纯空格值被吃成空串"),
    (5, "a" + chr(92) + "nb", "字面反斜杠+n 被还原成真换行 — 转义不可逆 (同族第三处)"),
])
def test_value_survives_roundtrip(one_table, rid, value, label):
    """凡库里存得下的文本值, 回放后必须一字不差 —— 备份的全部意义。"""
    s, d = _roundtrip(one_table, [(rid, f"{DAY} 10:00:00", "t", value, 42, 3.14)])
    assert _detail(d, rid) == value, label


def test_real_null_still_replays_as_null(one_table):
    """反面对照: 真 NULL 修完仍须是 NULL, 不许被修成字符串。"""
    s, d = _roundtrip(one_table, [(9, f"{DAY} 10:00:00", "t", None, 1, 1.0)])
    row = d.execute("SELECT detail, typeof(detail) FROM alerts WHERE id=9").fetchone()
    assert row[0] is None and row[1] == "null"


def test_newline_and_numeric_types_survive(one_table):
    """反面对照: 换行转义闭环与数值类型亲和已经是对的, 修别处不许把它们弄坏。"""
    s, d = _roundtrip(
        one_table, [(7, f"{DAY} 10:00:00", "t", "Traceback:\n  l1\n  l2", 42, 3.14)])
    assert _detail(d, 7) == "Traceback:\n  l1\n  l2"
    t = d.execute("SELECT typeof(n), typeof(x) FROM alerts WHERE id=7").fetchone()
    assert t == ("integer", "real")


# ------------------------------------------------------------ 验证面盲区

def test_fingerprint_distinguishes_null_from_text_none():
    """**对拍必须看得见它要抓的那种病。**

    `str(None) == str('None')` —— live 的文本 'None' 与 restored 的 NULL
    在旧 `_fingerprint` 下指纹完全相同。损坏与验证同时失效。
    """
    from ops_layer.incremental_backup import _fingerprint
    live = [(1, "None")]      # 库里真真正正存着四个字母 None
    restored = [(1, None)]    # 恢复出来是 NULL
    assert _fingerprint(live) != _fingerprint(restored), (
        "指纹把 NULL 与文本 'None' 判成同一行 —— 这正是本轮那条损坏的形态")


def test_fingerprint_still_matches_identical_rows():
    """反面对照: 相同行仍须同指纹 (别把 NULL 修成恒不等)。"""
    from ops_layer.incremental_backup import _fingerprint
    a = [(1, None, 2, 3.5, "x")]
    b = [(1, None, 2, 3.5, "x")]
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_still_catches_value_drift():
    """反面对照: 我上轮要求锁死的那条 (计数同值异) 不许退化。"""
    from ops_layer.incremental_backup import _fingerprint
    assert _fingerprint([(1, 29.43)]) != _fingerprint([(1, 99.9)])


# ------------------------------------------------------------ 判据面覆盖

def test_restore_coverage_exposes_uncovered_tables(tmp_path):
    """恢复判据面必须显式报出**不在增量面**的表。

    生产 89 表 / 增量 15 表 —— 「15/15 全绿」若不附上另外 74 张的去向,
    读者会把它当成全库结论。这与该模块自己的纪律同源:
    「空表 = 可见的 0 行条目, 不是缺席」。
    """
    from ops_layer.incremental_backup import restore_coverage
    db = str(tmp_path / "x.db")
    c = sqlite3.connect(db)
    c.execute(DDL)                                   # alerts: 在增量清单内
    c.execute("CREATE TABLE stock_pool (code TEXT)")  # 不在清单内
    c.execute("CREATE TABLE execution_stats (d TEXT)")
    c.commit()
    c.close()

    rep = restore_coverage(db)
    assert "stock_pool" in rep["uncovered"]
    assert "execution_stats" in rep["uncovered"]
    assert "alerts" in rep["covered"]
    assert rep["total"] == 3
    assert rep["uncovered_row_counts"]["stock_pool"] == 0


def test_verify_restore_marks_vacuous_comparisons(tmp_path, monkeypatch):
    """0 行 vs 0 行的 ✅ 是**空转恒真**, 必须与真对拍区分开。

    09-14 演练 15 表里有 8 表是 live=0/restored=0 —— 其中包括
    `daily_klines` 与 `factor_daily` 这两张**增量管道存在的全部理由**
    (回放窗口 09-12/09-13 是周六日, 而模块自述「周六日不跑, 无新数据」)。
    判 PASS 之前, 人得看得见哪几张其实什么也没比。
    """
    import ops_layer.incremental_backup as ib
    # alerts 在 FULL_DUMP_TABLES 里 —— verify_restore 对它忽略 dates 比全表,
    # 造不出「窗口内 0 行」。按日筛的表才能演出空转。(自己踩了自己在册的
    # 那条「夹具形态决定 bug 可见性」。)
    monkeypatch.setattr(ib, "INCREMENTAL_TABLES", [("alerts", "created_at", False)])
    monkeypatch.setattr(ib, "FULL_DUMP_TABLES", set())
    live = str(tmp_path / "live.db")
    rest = str(tmp_path / "rest.db")
    for p in (live, rest):
        c = sqlite3.connect(p)
        c.execute(DDL)
        c.execute("INSERT INTO alerts VALUES (1,?,'t','x',1,1.0)", (f"{DAY} 09:00:00",))
        c.commit()
        c.close()

    rep = ib.verify_restore(live, rest, [DAY])
    assert rep["alerts"]["vacuous"] is False
    rep2 = ib.verify_restore(live, rest, ["2099-01-01"])   # 窗口内无行
    assert rep2["alerts"]["vacuous"] is True, "0 vs 0 未被标成空转"
    assert rep2["alerts"]["count_match"] is True           # 仍为真, 只是空转
