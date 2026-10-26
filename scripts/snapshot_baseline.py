"""S0-7 基线快照脚本 — 生成系统状态基线 JSON

每 Sprint 出口可重跑, 与基线对比监控关键指标不回退:
- 33 张表行数与最新日期
- API /health 各层状态
- 系统资源(磁盘/内存)

用法: PYTHONPATH=src python scripts/snapshot_baseline.py [--label v0.19.0-s0]
"""
import json
import os
import shutil
import sqlite3
import sys
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "qs.db")
OUT_DIR = os.path.join(PROJECT_ROOT, "logs", "baselines")

CORE_TABLES = [
    "daily_klines", "factor_daily", "model_predictions", "stock_valuation",
    "stock_moneyflow", "stock_financials", "stock_limit_up_down", "stock_info",
    "concept_daily", "lhb_daily", "market_sentiment", "analyst_forecasts",
    "alpha_factors", "stock_pool", "action_records", "execution_stats",
    "post_exit_tracking", "intraday_checks", "alerts", "pipeline_runs",
]


def snapshot_tables() -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    tables = {}
    for t in CORE_TABLES:
        try:
            cnt = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{t}")')]
            datecol = next((c for c in ("date", "created_at") if c in cols), None)
            mx = cur.execute(f'SELECT MAX("{datecol}") FROM "{t}"').fetchone()[0] if datecol else None
            tables[t] = {"rows": cnt, "max_date": mx}
        except Exception as e:
            tables[t] = {"error": str(e)}
    conn.close()
    return tables


def snapshot_health() -> dict:
    try:
        env_path = os.path.join(PROJECT_ROOT, ".env")
        token = ""
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("QS_API_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"')
        req = urllib.request.Request(
            "http://127.0.0.1:8000/health",
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return {"status": data.get("status"),
                "layers": {l["name"]: l["status"] for l in data.get("layers", [])}}
    except Exception as e:
        return {"error": str(e)}


def main():
    label = "baseline"
    if "--label" in sys.argv:
        label = sys.argv[sys.argv.index("--label") + 1]
    os.makedirs(OUT_DIR, exist_ok=True)
    disk = shutil.disk_usage(os.path.expanduser("~"))
    snap = {
        "label": label,
        "version": "0.19.0-dev",
        "tables": snapshot_tables(),
        "health": snapshot_health(),
        "disk_used_pct": round(disk.used / disk.total * 100, 1),
        "db_size_gb": round(os.path.getsize(DB_PATH) / 1e9, 2),
    }
    from datetime import datetime
    snap["timestamp"] = datetime.now().isoformat()
    out = os.path.join(OUT_DIR, f"snapshot_{label}.json")
    with open(out, "w") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(json.dumps({"status": snap["health"], "tables_count": len(snap["tables"]),
                      "out": out}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
