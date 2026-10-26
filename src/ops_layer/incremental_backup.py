# -*- coding: utf-8 -*-
"""增量备份管道 — v1.0 判据 6/7 (备份异地化+恢复演练) 的核心件.

设计定谳 (统筹 09-14 夜接管, 亲执施工):
- 全备 (周一): VACUUM INTO 压缩快照 (现有 db_backup 管道保留, keep_daily=5)
- 增量 (周二~周五): 按表 dump 当日写入行 (单表 SQL dump, S0 纪律同款)
  - 小表全量行 (WAL 模式下当日行可精确按 date 列筛)
  - 大表 (daily_klines/factor_daily/concept_daily) 只 dump 当日行
- 周六日: 不跑 (无新数据)

回放语义 (审计官退场一刀, 2026-09-14): INSERT OR REPLACE — 全备后发生的
UPDATE (AUD-005 回填 18 笔的原形态) 必须以增量覆盖旧值; OR IGNORE 会让
被更新的行静默保留全备时刻的旧值, 计数对拍一路绿灯而数据是错的.

对拍双面 (同刀): verify_increment 每表 count_match + fingerprint_match
(排序元组 md5) — 计数面绿+值面红 = 假绿陷阱, 测试锁死.
"""
import gzip
import hashlib
import os
import sqlite3
from datetime import datetime

from common.logging import get_logger

logger = get_logger("ops.incremental_backup")

# 每日增量表清单: (表名, date 列名, 保留位 — 全量 dump 小表标记)
INCREMENTAL_TABLES = [
    ("daily_klines", "date", False),
    ("factor_daily", "date", False),
    ("stock_financials", "date", False),
    ("stock_valuation", "date", False),
    ("stock_moneyflow", "date", False),
    ("agent_checkpoints", "created_at", False),
    ("agent_predictions", "date", False),
    ("paper_trading_stats", "date", False),
    ("delivery_log", "created_at", False),
    ("pipeline_runs", "start_time", False),
    ("alerts", "created_at", False),
    ("action_records", "created_at", False),
    # 卡G2 (2026-09-20 用户裁定): popularity_daily/hot_concept_daily 停备 —
    # AdataSentimentPipeline 一次性种子死表 (09-12 各 100/20 行, 零消费者,
    # 管道零注册零运行), 每日备死数据 = 白做。代码保留不通电不退役,
    # REC 卡后评估是否作为推荐依据素材通电 (人气/题材热度对「有理有据」
    # 有用)。勿删代码; 测试锚 tests/unit/ops_layer/test_g2_dead_tables.py。
    ("stock_news", "date", False),
    # news_stock_links: 无日期列 (news_rowid PK) — 归周一全备, 不在增量面
]

# 全量 dump 的例外 (小到按日筛无意义)
FULL_DUMP_TABLES = {"paper_trading_stats", "alerts"}


class _SqlNull:
    """SQL NULL 的哨兵 —— **对象**, 不是字符串。

    旧实现把 NULL 解成字符串 "None", 回放时 `v == "None"` 映回 None ——
    于是库里真实存着的文本值 'None' 也被吃成 NULL (同族第二处: 文本 'NULL')。
    哨兵与数据同域就必然撞车; 换成域外对象, 撞车面从此为零。
    """
    __slots__ = ()

    def __repr__(self):
        return "SQL_NULL"

    def __eq__(self, other):
        return isinstance(other, _SqlNull)

    def __hash__(self):
        return hash("\x00SQL_NULL")


SQL_NULL = _SqlNull()

# 值面规范化里代表 NULL 的记号: 含 \x00, 任何 SQLite 文本值都不可能等于它
_NULL_MARK = "\x00NULL"


def _sql_literal(v) -> str:
    """SQL text literal escape: 反斜杠 -> 引号 -> 换行, 顺序不可颠倒。

    Prod alerts.detail holds real newlines (multi-line Traceback text);
    unescaped they break the one-line INSERT statement form and the
    parser splits at statement boundaries (2026-09-14 prod dry: 46/106).

    **先转义反斜杠本身**: 否则字面的 反斜杠+n 与真换行在 dump 里长得一模一样,
    转义不是单射, 往返就不可能保真 (Round2 复审实证)。
    """
    t = str(v).replace("\\", "\\\\").replace("'", "''")
    return (t.replace('\n', '\\n')
             .replace('\r', '\\r'))


def _sql_unescape(t: str) -> str:
    """_sql_literal 的逆 —— 左到右单趟扫, 反斜杠自身先被认出来。

    不能用连续 .replace(): 那样 `\\n` (转义过的字面反斜杠 + n) 会被
    第二趟当成换行, 逆函数就不再是逆。
    """
    out, i, n = [], 0, len(t)
    while i < n:
        ch = t[i]
        if ch == "\\" and i + 1 < n:
            nxt = t[i + 1]
            out.append({"n": "\n", "r": "\r", "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _cell(v) -> str:
    """值面规范化: NULL 走不可撞记号, 其余 str()。

    旧实现直接 str(v) —— 而 `str(None) == str('None') == 'None'`, 于是
    live 的文本 'None' 与 restored 的 NULL 指纹完全相同: **对拍恰好对
    它该抓的那种损坏是盲的**。验证面看不见自己要抓的病, 比没有验证更坏。
    """
    if v is None or isinstance(v, _SqlNull):
        return _NULL_MARK
    return str(v)


def _fingerprint(rows: list) -> str:
    """行集值指纹: 排序元组 md5. 值面对拍引擎 (计数同+值异 = 恢复出旧账)."""
    canonical = sorted(tuple(_cell(v) for v in row) for row in rows)
    blob = "\n".join("|".join(r) for r in canonical).encode("utf-8")
    return hashlib.md5(blob).hexdigest()


def run_incremental_backup(db_path: str, backup_dir: str,
                           date: str | None = None) -> dict:
    """按表 dump 当日行. 返回 {table: rows_dumped} 汇总 + 产物路径.

    幂等: 重跑同一天会覆盖同名产物 (gzip tmp+rename 原子性).
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    day_dir = os.path.join(backup_dir, f"incremental/{date}")
    os.makedirs(day_dir, exist_ok=True)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    summary = {}
    manifest_lines = [f"# incremental backup {date}", f"# db: {db_path}"]

    for table, date_col, _ in INCREMENTAL_TABLES:
        try:
            if table in FULL_DUMP_TABLES:
                rows = con.execute(f"SELECT * FROM {table}").fetchall()
            else:
                rows = con.execute(
                    f"SELECT * FROM {table} WHERE {date_col} LIKE ?",
                    (f"{date}%",)
                ).fetchall()

            if not rows:
                summary[table] = 0
                continue

            # 产物: gzip SQL INSERT 语句 (REPLACE 语义 — 审计官退场一刀)
            out_path = os.path.join(day_dir, f"{table}.sql.gz")
            tmp = out_path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(f"-- incremental dump: {table} @ {date}\n")
                for row in rows:
                    cols = ", ".join(row.keys())
                    vals = ", ".join(
                        "NULL" if v is None
                        else "'" + _sql_literal(v) + "'"
                        for v in row
                    )
                    f.write(
                        f"INSERT OR REPLACE INTO {table} ({cols}) "
                        f"VALUES ({vals});\n")
            os.replace(tmp, out_path)

            md5 = hashlib.md5(open(out_path, "rb").read()).hexdigest()[:12]
            size = os.path.getsize(out_path)
            summary[table] = len(rows)
            manifest_lines.append(
                f"{table}: {len(rows)} rows, {size} B, md5 {md5}")

        except sqlite3.Error as e:
            logger.warning(f"incremental dump {table} failed: {e}")
            summary[table] = -1

    con.close()

    # manifest
    mf = os.path.join(day_dir, "MANIFEST.txt")
    with open(mf, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest_lines) + "\n")

    total_rows = sum(v for v in summary.values() if v > 0)
    logger.info(
        f"incremental backup {date}: {len(summary)} tables, "
        f"{total_rows} rows total -> {day_dir}")
    return {"summary": summary, "dir": day_dir, "total_rows": total_rows}


def verify_increment(db_path: str, inc_dir: str, date: str) -> dict:
    """dump 对拍 (mode=ro 只读). 每表双面:

    - count_match: dump 行数 == 库内当日行数
    - fingerprint_match: dump 值指纹 == 库内当日行值指纹
      (审计官退场一刀: 计数同+值异 = UPDATE 丢失/漂移, 一律 mismatch)
    报告完整性 = 清单全表 (空表 = 可见的 0 行条目, 不是缺席 — AUD-030 同族防线).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    report = {}
    for table, date_col, _ in INCREMENTAL_TABLES:
        fn = os.path.join(inc_dir, f"{table}.sql.gz")
        if not os.path.exists(fn):
            # 当日 0 行表无产物 — 显式 0==0 条目
            report[table] = {"dumped": 0, "actual": 0,
                              "count_match": True,
                              "fingerprint_match": True}
            continue
        try:
            with gzip.open(fn, "rt", encoding="utf-8") as f:
                dump_lines = sum(1 for l in f if l.startswith("INSERT"))
            if table in FULL_DUMP_TABLES:
                rows = con.execute(f"SELECT * FROM {table}").fetchall()
            else:
                rows = con.execute(
                    f"SELECT * FROM {table} WHERE {date_col} LIKE ?",
                    (f"{date}%",)).fetchall()
            actual = len(rows)
            # 值面: 从 dump 语句反解值元组, 与库内行指纹对拍
            # (dump 语句形态: INSERT OR REPLACE INTO t (cols) VALUES (...);)
            dump_rows = _parse_dump_values(fn)
            fp_dump = _fingerprint(dump_rows)
            fp_live = _fingerprint([tuple(r) for r in rows])
            report[table] = {
                "dumped": dump_lines, "actual": actual,
                "count_match": dump_lines == actual,
                "fingerprint_match": fp_dump == fp_live,
            }
        except Exception as e:
            report[table] = {"error": str(e)[:80]}
    con.close()
    return report


def _parse_dump_values(fn: str) -> list:
    """从 dump 文件反解值元组 (对拍引擎用, 只解析 VALUES (...); 段)."""
    rows = []
    with gzip.open(fn, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("INSERT"):
                continue
            vp = line.find("VALUES (")
            if vp < 0:
                continue
            payload = line[vp + len("VALUES ("):]
            if payload.endswith(");"):
                payload = payload[:-2]
            rows.append(_split_sql_values(payload))
    return rows


def _split_sql_values(payload: str) -> tuple:
    """按 SQL 语义切 VALUES 内的值。加引号与否**必须分开记**。

    两处旧病都源于「切完就不知道它原来有没有引号」:
      - `"".join(buf).strip()` 无条件剥离: 本意是去掉分隔符 `, ` 的空格,
        却连**引号内部**的真实空白一起吃掉 ('  padded  ' -> 'padded')。
        引号内的内容是数据, 一个字符都不许动。
      - `v == "NULL"` 判 NULL: 加了引号的文本 'NULL' 也被判成 SQL NULL。
        未加引号的裸 NULL 才是 SQL NULL, 这个区分只有在切的当下才知道。
    返回值: 文本/数值令牌为 str, SQL NULL 为 SQL_NULL 哨兵对象。
    """
    parts, buf, in_quote, quoted = [], [], False, False
    i = 0
    while i < len(payload):
        ch = payload[i]
        if in_quote:
            if ch == "'" and i + 1 < len(payload) and payload[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            if ch == "'":
                in_quote = False
            else:
                buf.append(ch)
        else:
            if ch == "'":
                in_quote = True
                quoted = True
                buf = []          # 丢掉引号**前**的分隔空白, 引号内的一概不碰
            elif ch == ",":
                parts.append(("".join(buf), quoted))
                buf, quoted = [], False
            else:
                buf.append(ch)
        i += 1
    parts.append(("".join(buf), quoted))

    out = []
    for text, was_quoted in parts:
        if was_quoted:
            out.append(_sql_unescape(text))
        elif text.strip() == "NULL":
            out.append(SQL_NULL)
        else:
            out.append(text.strip())
    return tuple(out)


def verify_restore(live_db_path: str, restored_db_path: str,
                    dates: list[str]) -> dict:
    """恢复演练终判面: 恢复库 vs 生产库值面对拍 (v1.0 判据 7).

    流程 (审计官 HANDOVER 约束: 不走 DDL 重建+回灌 — 文件复制 + REPLACE 回放):
    1. 全备 gz 文件复制解压为临时恢复库
    2. 逐日回放增量 dump (INSERT OR REPLACE — UPDATE 也覆盖)
    3. 本函数: 恢复库与生产库按 dates 逐表值面 (count+fingerprint) 对拍

    终判 = 全部 count_match AND fingerprint_match.
    """
    live = sqlite3.connect(f"file:{live_db_path}?mode=ro", uri=True)
    live.row_factory = sqlite3.Row
    restored = sqlite3.connect(f"file:{restored_db_path}?mode=ro", uri=True)
    restored.row_factory = sqlite3.Row
    # 审计官退场补刀②: REPLACE = DELETE+INSERT, 若库开了 PRAGMA foreign_keys
    # 则父行级联删子行且不再回来. QS 生产库默认关, 但恢复流程若在某步开了它,
    # 行为就变 — 机器断言锁死, 不留给记性. mode=ro 连接默认关, 断言防外部打开.
    for _con, _tag in ((live, "live"), (restored, "restored")):
        fk = _con.execute("PRAGMA foreign_keys").fetchone()[0]
        if fk:
            raise RuntimeError(
                f"verify_restore: {_tag} 库 foreign_keys={fk} — "
                f"REPLACE 回放会触发级联删, 恢复语义不可判 (审计官补刀②)")
    report = {}
    for table, date_col, _ in INCREMENTAL_TABLES:
        try:
            if table in FULL_DUMP_TABLES:
                # 全量 dump 表: 库内全表对拍 (dates 不参与)
                l_rows = live.execute(f"SELECT * FROM {table}").fetchall()
                r_rows = restored.execute(f"SELECT * FROM {table}").fetchall()
            else:
                conds = " OR ".join(
                    f"{date_col} LIKE ?" for _ in dates)
                params = [f"{d}%" for d in dates]
                l_rows = live.execute(
                    f"SELECT * FROM {table} WHERE {conds}", params).fetchall()
                r_rows = restored.execute(
                    f"SELECT * FROM {table} WHERE {conds}", params).fetchall()
            report[table] = {
                "live": len(l_rows), "restored": len(r_rows),
                "count_match": len(l_rows) == len(r_rows),
                "fingerprint_match": (
                    _fingerprint([tuple(r) for r in l_rows])
                    == _fingerprint([tuple(r) for r in r_rows])),
                # 0 行 vs 0 行的 ✅ 是**空转恒真**, 不是通过。09-14 演练 15 表
                # 里有 8 表如此 (回放窗口 09-12/09-13 是周六日, 而本模块自述
                # 「周六日不跑, 无新数据」) —— 含 daily_klines / factor_daily
                # 这两张增量管道存在的全部理由。判 PASS 前人得看得见。
                "vacuous": len(l_rows) == 0 and len(r_rows) == 0,
            }
        except sqlite3.Error as e:
            report[table] = {"error": str(e)[:80]}
    live.close()
    restored.close()
    return report


def replay_increment(inc_dir: str, db_path: str) -> dict:
    """增量回放 (恢复演练用): dump 语句 -> 真值 -> 参数绑定执行.

    转义闭环: dump 端 _sql_literal 转义 (引号翻倍+换行转义), 本函数用与
    _split_sql_values 同一的解析机器还原真值, 再走绑定参数执行 — 值经
    参数绑定传入, 不再经过 SQL 字面量, 转义/还原只有一条路径.

    直接 execute 原语句的病 (2026-09-14 演练亲踩): dump 里的换行转义
    序列会被 SQLite 原样存为两字符 '\n', 恢复库值面与生产库真换行
    不符 — verify_restore 值指纹红.
    """
    con = sqlite3.connect(db_path)
    replayed = {}
    import os as _os
    for fn in sorted(_os.listdir(inc_dir)):
        if not fn.endswith(".sql.gz"):
            continue
        table = fn.replace(".sql.gz", "")
        n = 0
        with gzip.open(_os.path.join(inc_dir, fn), "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("INSERT"):
                    continue
                lp = line.find("(")
                vp = line.find("VALUES (")
                cols = [c.strip() for c in
                        line[lp:vp - 1].strip("() ").split(",")]
                payload = line[vp + len("VALUES ("):]
                if payload.endswith(");"):
                    payload = payload[:-2]
                vals = _split_sql_values(payload)
                ph = ", ".join("?" for _ in vals)
                con.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
                    f"VALUES ({ph})",
                    tuple(None if isinstance(v, _SqlNull) else v for v in vals))
                n += 1
        con.commit()
        replayed[table] = n
    con.close()
    return replayed


def restore_coverage(db_path: str, count_rows: bool = True) -> dict:
    """恢复判据面覆盖账: 哪些表在增量面, 哪些**只能恢复到全备时刻**。

    为什么必须有: 09-14 演练报「15 表值面全绿」并判 PASS, 而生产 89 张表里
    有 74 张根本不在 INCREMENTAL_TABLES —— 其中 stock_pool / post_exit_tracking
    / execution_stats / model_predictions / factor_weights 是执行与模型的核心账。
    (AUD-005 那 18 笔回填的宿主表正是 stock_pool: 当初据以主张 REPLACE 语义的
     那个例子, 它的表根本不在 dump 面。)
    「15/15 绿」是对 15 张表说的, 不是对恢复能力说的 —— 缺口不报出来, 下一个人
    就会把它读成全库结论。这与本模块已有的「空表 = 可见的 0 行条目, 不是缺席」
    是同一条纪律, 只是从表**内**推到表**间**。

    count_rows=True 会对每张缺席表做一次 COUNT(*) (全扫) —— 演练/报告用,
    不是热路径。concept_daily 量级下会明显耗时, 不需要行数时传 False。
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    inc = {t for t, _, _ in INCREMENTAL_TABLES}
    covered = [t for t in names if t in inc]
    uncovered = [t for t in names if t not in inc]
    counts = {}
    if count_rows:
        for t in uncovered:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = -1
    con.close()
    logger.info(
        f"restore coverage: {len(covered)}/{len(names)} 表在增量面, "
        f"{len(uncovered)} 表仅能恢复到全备时刻")
    return {"total": len(names), "covered": covered, "uncovered": uncovered,
            "uncovered_row_counts": counts}
