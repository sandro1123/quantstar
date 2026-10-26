#!/usr/bin/env python3
"""S2-F2.7 stock_info industry 补全 + 退市标记脚本

数据源: tushare stock_basic (industry 字段) → 东财行业板块备链
退市判定: 名称含"退" → is_active=0; delist_date 列缺失时 O(1) 添加
用法:
  1. 备份: sqlite3 data/qs.db ".dump stock_info > backups/stock_info_s2.sql"
  2. 隔离验证: python scripts/backfill_stock_info.py --db <隔离库> --dry-run
  3. 正式执行: python scripts/backfill_stock_info.py --db data/qs.db
证据: 追加 logs/repair_evidence_s2.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "stock_info"


def _fetch_tushare_basic() -> tuple[dict[str, str], dict[str, str]]:
    """tushare stock_basic → (industry_map, delist_map); 免费版限流 1次/分钟, 带重试。
    L 列表失败视为致命错误直接抛出（覆盖率达标依赖 L 全量）。
    """
    import time

    import tushare as ts
    pro = ts.pro_api()
    frames = []
    for i, status in enumerate(("L", "D")):
        if i > 0:
            time.sleep(62)
        df = None
        for attempt in range(4):
            try:
                df = pro.stock_basic(exchange="", list_status=status,
                                     fields="ts_code,symbol,name,industry,delist_date")
                break
            except Exception as e:
                print(f"  tushare stock_basic({status}) attempt {attempt + 1}/4 failed: {str(e)[:120]}")
                if attempt < 3:
                    time.sleep(62)
        if status == "L":
            if df is None or len(df) == 0:
                raise RuntimeError("tushare stock_basic(L) 拉取失败, industry 回填终止")
        if df is not None and len(df) > 0:
            frames.append(df)
    indmap: dict[str, str] = {}
    demap: dict[str, str] = {}
    for df in frames:
        for _, r in df.iterrows():
            code = str(r.get("symbol", "") or "")
            if not code:
                continue
            if r.get("industry"):
                indmap.setdefault(code, str(r["industry"]))
            dl = str(r.get("delist_date", "") or "")
            if dl:
                demap[code] = dl
    return indmap, demap


def _fetch_sw_industry_map() -> dict[str, str]:
    """备链: 申万一级行业成分 (ak.index_component_sw), 不依赖东财/巨潮。"""
    import time

    import akshare as ak
    first = ak.sw_index_first_info()
    indmap: dict[str, str] = {}
    for _, r in first.iterrows():
        code = str(r["行业代码"]).split(".")[0]
        name = str(r["行业名称"])
        try:
            cons = ak.index_component_sw(symbol=code)
            if cons is None or len(cons) == 0:
                continue
            for _, row in cons.iterrows():
                sc = str(row.get("证券代码", "") or "")
                if sc:
                    indmap.setdefault(sc, name)
            print(f"  申万 {name}({code}): {len(cons)} 只")
            time.sleep(0.5)
        except Exception as e:
            print(f"  申万 {name}({code}) 成分失败: {str(e)[:100]}")
            continue
    return indmap


def _ensure_columns(conn) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]
    added = []
    if "delist_date" not in cols:
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN delist_date TEXT DEFAULT ''")
        added.append("delist_date")
    if added:
        conn.commit()
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description="stock_info industry/退市回填")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--evidence", default="logs/repair_evidence_s2.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    missing = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE industry IS NULL OR industry = ''"
    ).fetchone()[0]
    delist_names = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE name LIKE '%退%'"
    ).fetchone()[0]
    missing_active = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE name LIKE '%退%' AND is_active != 0"
    ).fetchone()[0]

    print(f"[{args.db}] {TABLE}: 共 {total} 行, industry 缺失 {missing}, "
          f"名称含'退' {delist_names}(其中 is_active!=0 的 {missing_active})")

    if args.dry_run:
        print("[dry-run] 不拉取外部源, 不写入")
        conn.close()
        return 0

    indmap: dict[str, str] = {}
    demap: dict[str, str] = {}
    sources_used = []
    try:
        indmap, demap = _fetch_tushare_basic()
        sources_used.append(f"tushare:{len(indmap)}")
    except Exception as e:
        print(f"  tushare 链失败: {str(e)[:150]}")
    if len(indmap) < 4000:
        print("  tushare industry 覆盖不足, 启用申万备链...")
        sw = _fetch_sw_industry_map()
        for code, name in sw.items():
            indmap.setdefault(code, name)
        sources_used.append(f"sw:{len(sw)}")
    added_cols = _ensure_columns(conn)

    industry_updated = 0
    for code, industry in indmap.items():
        cur = conn.execute(
            f"UPDATE {TABLE} SET industry = ? WHERE code = ? "
            "AND (industry IS NULL OR industry = '')",
            (industry, code),
        )
        industry_updated += cur.rowcount

    delist_marked = 0
    rows = conn.execute(f"SELECT code FROM {TABLE} WHERE name LIKE '%退%' AND is_active != 0").fetchall()
    for r in rows:
        conn.execute(f"UPDATE {TABLE} SET is_active = 0, delist_date = COALESCE(NULLIF(delist_date, ''), ?) WHERE code = ?",
                     (demap.get(r["code"], ""), r["code"]))
        delist_marked += 1
    conn.commit()

    after_missing = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE industry IS NULL OR industry = ''"
    ).fetchone()[0]
    coverage = (total - after_missing) / total if total else 0.0

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "sprint": "S2",
        "task": "F2.7_stock_info_backfill",
        "db": args.db,
        "industry_updated": industry_updated,
        "industry_missing_after": after_missing,
        "coverage_pct": round(coverage * 100, 2),
        "delist_marked": delist_marked,
        "columns_added": added_cols,
        "sources_used": sources_used,
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    print(f"回填完成: industry 更新 {industry_updated}, 缺失剩余 {after_missing} "
          f"(覆盖率 {coverage:.1%}), 退市标记 {delist_marked}; 证据 {args.evidence}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
