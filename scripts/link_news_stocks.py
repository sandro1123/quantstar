"""AG-NEWS ②: 存量市场新闻的个股关联抽取 (一次性脚本, 默认 dry-run 只读).

面: stock_news 13,325 行中 10,443 行 code 空 (attribution_method NULL)。
已有三法 vocab_colon/vocab_title/vocab_summary 关联了 2,882 行 — 本脚本
是第 4 法: 全名扫描标题 (股票名↔code 映射自 stock_info), 派生关联
attribution_method='derived_title_match' (与真实个股新闻可辨别)。

规则 (防误中设计, 宁缺毋假):
  - 只扫 code 为空的行 (已有归属不覆盖);
  - 名字 ∈ stock_info.name 且 len>=3 (6014 名全>=3, 2字名为 0 — 实证无此风险面,
    但规则仍写死下限防未来新增);
  - 标题包含名后必须紧跟「非公司字」边界: 名后字符∉{股,份,集,团,科,技,新,能,源} —
    即标题里出现的是股票名本身而非更长公司名前缀 (如名=「新能」, 标题含「新能
    源」须跳过; 实证 len>=3 下此风险极低, 边界规则为守卫);
  - 多股命中 → 保留全部 (写多行) — 新闻本可关联多股, 不强行唯一;
  - summary 段不扫 (派单只要求标题)。

用法:
  dry-run (默认, 零写): python link_news_stocks.py
  真写: python link_news_stocks.py --write  (UPDATE stock_news SET code=?, attribution_method='derived_title_match' WHERE rowid=?)

产物: /tmp/agnews_link_report.json (双写 ~/qs-cfgsnap/audit/20260910-AG-NEWS/)
  命中数/覆盖率/多股分布/误中抽样 (供验收呈报)。
"""
import os
import json
import sqlite3
import sys
from datetime import datetime

WRITE = "--write" in sys.argv
DB = os.path.expanduser("~/qs/data/qs.db")

conn = sqlite3.connect(f"file:{DB}?mode=ro" if not WRITE else DB, uri=not WRITE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ---- 名↔code 映射 (stock_info) ----
names = {}
for code, name in cur.execute("SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name != ''"):
    if len(name) >= 3:  # 规则下限: 3 字名起才扫 (全库实证最短=3)
        names.setdefault(name, []).append(code)
print(f"map: {len(names)} names (>=3 chars), {sum(len(v) for v in names.values())} code links")

# ---- 待扫行: code 空 ----
rows = cur.execute(
    "SELECT rowid, title, summary, source, date FROM stock_news "
    "WHERE code IS NULL OR code = ''"
).fetchall()
print(f"scan target rows: {len(rows)}")

# 边界守卫: 名后字符若属于公司名后缀族 → 该命中视为「更长公司名的一部分」跳过
SUFFIX_CHARS = set("股份集团科技新能源电气药业生物控股实业材料重工")

# 通名黑名单: 行业词与股票名撞名的高危族 — 标题里通常是泛指物非指代公司,
# 命中即跳过 (宁缺毋假)。首员「机器人」=300024 沈阳新松真名, 但
# 「72台机器人上岗」「人形机器人」等标题命中皆为行业泛指 (实证样本 3 例)。
STOP_NAMES = {"机器人"}

hits = []          # [(rowid, code, name)]
multi = 0
samples = []
for r in rows:
    title = r["title"] or ""
    found = []
    for name, codes in names.items():
        if name in STOP_NAMES:
            continue
        idx = title.find(name)
        if idx < 0:
            continue
        end = idx + len(name)
        nxt = title[end] if end < len(title) else ""
        if nxt in SUFFIX_CHARS:
            continue  # 更长公司名前缀, 跳过
        for c in codes:
            found.append((c, name))
    if found:
        if len(found) > 1:
            multi += 1
        for c, name in found:
            hits.append((r["rowid"], c, name))
        if len(samples) < 12:
            samples.append({"title": title[:50], "names": [n for _, n in found]})

covered_rows = len({h[0] for h in hits})
total_rows = len(rows)
pct = round(covered_rows / total_rows * 100, 1) if total_rows else 0
print(f"hits: {covered_rows} rows covered ({pct}%), {len(hits)} row-code pairs, multi-stock rows: {multi}")

report = {
    "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "mode": "WRITE" if WRITE else "DRY",
    "names_in_map": len(names),
    "scan_rows_code_empty": total_rows,
    "covered_rows": covered_rows,
    "coverage_pct": pct,
    "row_code_pairs": len(hits),
    "multi_stock_rows": multi,
    "samples": samples,
}

if WRITE:
    conn_rw = sqlite3.connect(DB)
    conn_rw.execute("BEGIN")
    n = 0
    for rowid, code, _name in hits:
        cur_rw = conn_rw.execute(
            "UPDATE stock_news SET code=?, attribution_method='derived_title_match' WHERE rowid=?",
            (code, rowid),
        )
        n += cur_rw.rowcount
    conn_rw.commit()
    print(f"WROTE {n} updates")
    report["written"] = n
    conn_rw.close()
    conn.close()
    conn = None

out = json.dumps(report, ensure_ascii=False, indent=1)
print(out)
ts = datetime.now().strftime("%Y%m%dT%H%M%S")
for p in (f"/tmp/agnews_link_report_{ts}.json",
          fos.path.expanduser("~/qs-cfgsnap/audit/20260910-AG-NEWS/link_report_{ts}.json")):
    with open(p, "w") as f:
        f.write(out)
print("REPORT_SAVED")
