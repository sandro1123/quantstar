"""AG-NEWS ④b: news_stock_links 多对多关联表 — 迁移+回填。

设计 (随交付回执呈统筹裁):
  表: news_stock_links (news_rowid INTEGER, code TEXT, match_method TEXT,
                        PRIMARY KEY (news_rowid, code))
  - news_rowid 关联 stock_news.rowid (行稳定性锚 — rowid 在 INSERT/DELETE
    间稳定, stock_news 无整型主键; 若未来加 PK 需同步迁移此列语义);
  - code: 关联股票 (一行新闻可多股 — 169 行多股面的全关联承载);
  - match_method: vocab_colon / vocab_title / vocab_summary /
    derived_title_match / per_stock_em (与 stock_news.attribution_method
    同词表, 双写不互斥)。
  单 code 列 (stock_news.code) 保留: 向后兼容 (get_news 等旧读面), 语义=
    "首个/最强命中", links 表承载全量。**退役与否留统筹裁** — 本版保留。

回填 backfill_news_stock_links: 把既有 attribution_method 非空行的
(rowid, code, method) 全灌入 links (增量幂等 INSERT OR IGNORE)。
真写库前必须 S0 单表 dump 先行 (纪律不变)。
"""
from __future__ import annotations

LINKED_METHODS = ("vocab_colon", "vocab_title", "vocab_summary",
                  "derived_title_match", "per_stock_em")


def apply_news_stock_links_migration(conn) -> None:
    """建表 (幂等)。不碰 stock_news 原表 — 零 DDL 风险面。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_stock_links (
            news_rowid INTEGER NOT NULL,
            code TEXT NOT NULL,
            match_method TEXT NOT NULL,
            PRIMARY KEY (news_rowid, code)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_stock_links_code
        ON news_stock_links (code)
    """)
    conn.commit()


def backfill_news_stock_links(conn) -> int:
    """既有归属行灌入 links 表 (增量幂等)。返回写入对数。"""
    apply_news_stock_links_migration(conn)
    placeholders = ", ".join("?" for _ in LINKED_METHODS)
    rows = conn.execute(
        f"SELECT rowid, code, attribution_method FROM stock_news "
        f"WHERE code IS NOT NULL AND code != '' "
        f"AND attribution_method IN ({placeholders})",
        LINKED_METHODS,
    ).fetchall()
    n = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_stock_links (news_rowid, code, match_method) "
            "VALUES (?, ?, ?)",
            (r["rowid"], r["code"], r["attribution_method"]),
        )
        n += cur.rowcount
    conn.commit()
    return n


# AG-NEWS ③GO (2026-09-12): 第二遍标题全名扫描 — per_stock_em 多股名行补全.
# 实证: 240 行里 5+ 行标题含 2~3 只股票名 (行业资金流入榜), links 只关联
# 拉取目标股. 本遍用 link_news_stocks 同款名映射+守卫 (>=3 字名/后缀边界/
# 通名黑名单) 扫 per_stock_em 行标题, 其余股补入 links (OR IGNORE 幂等).
_SECOND_PASS_METHOD = "title_scan_2nd"
_STOP_NAMES = {"机器人"}
_SUFFIX_CHARS = set("股份集团科技新能源电气药业生物控股实业材料重工")


def second_pass_title_scan(conn) -> int:
    """对 per_stock_em 行做标题全名扫描, 补全多股名关联. 返回新写对数 (幂等)."""
    rows = conn.execute(
        "SELECT rowid, title FROM stock_news WHERE attribution_method = 'per_stock_em'"
    ).fetchall()

    names: dict[str, list[str]] = {}
    try:
        for code, name in conn.execute(
            "SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name != ''"
        ).fetchall():
            if len(name) >= 3:
                names.setdefault(name, []).append(code)
    except Exception:
        return 0  # stock_info 缺表 — 诚实降级零写

    n = 0
    for r in rows:
        rowid = r["rowid"] if not isinstance(r, tuple) else r[0]
        title = (r["title"] if not isinstance(r, tuple) else r[1]) or ""
        for name, codes in names.items():
            if name in _STOP_NAMES:
                continue
            i = title.find(name)
            if i < 0:
                continue
            end = i + len(name)
            nxt = title[end] if end < len(title) else ""
            if nxt in _SUFFIX_CHARS:
                continue
            for c in codes:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO news_stock_links "
                    "(news_rowid, code, match_method) VALUES (?, ?, ?)",
                    (rowid, c, _SECOND_PASS_METHOD),
                )
                n += cur.rowcount
    conn.commit()
    return n
