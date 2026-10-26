"""stock_news 个股归属回填/增量 — Phase 0 词表路线 (P2-19)

用法:
  python scripts/attribute_news.py --dry-run   # 只报数不写库 (默认)
  python scripts/attribute_news.py --apply     # 实际写库 (统筹窗口放行后)

写库内容: ①ALTER TABLE stock_news ADD COLUMN attribution_method (幂等)
②未归属行 (code IS NULL OR '') 按词表漏斗回填 code + method。
已归属行不覆盖。幂等: 重跑只处理剩余未归属行。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from common.database import Database
from common.logging import get_logger, setup_logging
from data_layer.news_attribution import NewsAttributor

setup_logging(profile="prod")
logger = get_logger("scripts.attribute_news")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库 (默认 dry-run 只报数)")
    args = ap.parse_args()

    db = Database()
    attr = NewsAttributor(db)
    vocab_n = attr.load_vocab()
    conn = db.get_connection()

    rows = conn.execute(
        "SELECT rowid, title, summary FROM stock_news WHERE code IS NULL OR code = ''"
    ).fetchall()
    logger.info(f"待归属: {len(rows)} 行 (词表 {vocab_n} 名)")

    hits: dict[str, list] = {"vocab_colon": [], "vocab_title": [], "vocab_summary": []}
    for r in rows:
        res = attr.attribute(r["title"] or "", r["summary"] or "")
        if res:
            hits[res.method].append((r["rowid"], res.code, (r["title"] or "")[:40]))

    total_hit = sum(len(v) for v in hits.values())
    print(f"== 归属报数 ({'APPLY' if args.apply else 'DRY-RUN'}) ==")
    print(f"待归属: {len(rows)}  命中: {total_hit} ({total_hit / max(1, len(rows)) * 100:.1f}%)")
    for m in ("vocab_colon", "vocab_title", "vocab_summary"):
        print(f"  {m}: {len(hits[m])}")
        for rid, code, title in hits[m][:3]:
            print(f"    样例 rowid={rid} → {code} | {title}")

    if not args.apply:
        print("DRY-RUN 结束, 未写库。加 --apply 实际回填 (需统筹窗口放行)。")
        return

    # 幂等加列
    existing = {r[1] for r in conn.execute("PRAGMA table_info(stock_news)")}
    if "attribution_method" not in existing:
        conn.execute("ALTER TABLE stock_news ADD COLUMN attribution_method TEXT")
        logger.info("ALTER TABLE stock_news ADD COLUMN attribution_method")

    updated = 0
    for method, items in hits.items():
        for rid, code, _ in items:
            conn.execute(
                "UPDATE stock_news SET code = ?, attribution_method = ? "
                "WHERE rowid = ? AND (code IS NULL OR code = '')",
                (code, method, rid),
            )
            updated += 1
    conn.commit()
    logger.info(f"回填完成: {updated} 行")
    print(f"APPLY 完成: {updated} 行回填")


if __name__ == "__main__":
    main()
