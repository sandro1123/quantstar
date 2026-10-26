"""运维仪表盘 Bitable 同步（c 轨主脚本，对应 ops.yaml dashboard_bitable_sync）。

从 qs.db（mode=ro）与 logs/ 聚合 → 写 Bitable 4 表：
  T1 📊 管道运行矩阵  env FEISHU_BITABLE_DASHBOARD_JOB_TABLE   （按日 replace）
  T2 📋 运维日报摘要  env FEISHU_BITABLE_DASHBOARD_DIGEST_TABLE（按日 replace）
  T3 🔔 告警台账      env FEISHU_BITABLE_DASHBOARD_ALERT_TABLE （alert_id 去重增量）
  T4 📈 信号总览      env FEISHU_BITABLE_SIGNAL_TABLE          （最新信号日 replace）

铁律纪律：
  #11 自载 dotenv（cron 直驱不走 __main__）；
  #12 每段成败必落 delivery_log（写库走独立读写连接，只 INSERT delivery_log）；
  读库一律 mode=ro 短连接；大表 SQL 先聚合。
用法：
  ~/qs/.venv/bin/python ~/qs/scripts/build_ops_bitable.py [--days 90] [--dry-run]
"""
import argparse
import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

QS = Path(os.path.expanduser("~/qs"))
DB_URI = f"file:{QS}/data/qs.db?mode=ro"
E2E_LOG = QS / "logs" / "e2e_verification.jsonl"
API = "https://open.feishu.cn/open-apis"

# 铁律 #11：自带 dotenv
sys.path.insert(0, str(QS / "src"))
load_dotenv(QS / ".env")

# T1 作业白名单（施工稿 14 作业）
JOBS = [
    "morning_brief", "pre_market_refresh", "auction_parse", "morning_review",
    "midday_brief", "daily_data_pipeline", "factor_update", "ic_feedback",
    "quality_check", "freshness_audit", "daily_backtest", "evening_report",
    "daily_ops_report", "paper_trading",
]

NOW = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def bj_today() -> str:
    return _dt.date.today().isoformat()


def ro_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_URI, uri=True, timeout=10)


def get_token() -> str | None:
    r = requests.post(
        f"{API}/auth/v3/tenant_access_token/internal",
        json={"app_id": os.getenv("FEISHU_APP_ID"),
              "app_secret": os.getenv("FEISHU_APP_SECRET")},
        timeout=10)
    return r.json().get("tenant_access_token")


def api_call(token: str, method: str, path: str, payload=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, f"{API}{path}", headers=headers,
                         json=payload, timeout=15)
    return r.json()


def fetch_all_records(token, app, table, field_names):
    """拉全表指定列，返回 [(record_id, fields)]。"""
    out, page_token = [], ""
    while True:
        body = {"field_names": field_names, "page_size": 500}
        if page_token:
            body["page_token"] = page_token
        res = api_call(token, "POST",
                       f"/bitable/v1/apps/{app}/tables/{table}/records/search", body)
        if res.get("code") != 0:
            raise RuntimeError(f"search {table}: code={res.get('code')} msg={res.get('msg')}")
        data = res.get("data", {})
        for item in data.get("items", []):
            out.append((item["record_id"], item.get("fields", {})))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return out


def batch_delete(token, app, table, ids):
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        res = api_call(token, "POST",
                       f"/bitable/v1/apps/{app}/tables/{table}/records/batch_delete",
                       {"records": chunk})
        if res.get("code") != 0:
            raise RuntimeError(f"batch_delete {table}: code={res.get('code')}")


def batch_create(token, app, table, rows):
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        res = api_call(token, "POST",
                       f"/bitable/v1/apps/{app}/tables/{table}/records/batch_create",
                       {"records": [{"fields": r} for r in chunk]})
        if res.get("code") != 0:
            raise RuntimeError(f"batch_create {table}: code={res.get('code')} msg={res.get('msg')}")


def text_of(v) -> str:
    """Bitable search 返回 Text 列是分段数组 [{'text':..,'type':'text'}]，兼容两种形态。"""
    if isinstance(v, list):
        return "".join(seg.get("text", "") for seg in v if isinstance(seg, dict))
    return "" if v is None else str(v)


# ---------- 聚合 ----------

def build_t1(rows_days: int):
    """T1：白名单作业近 N 天逐次运行，按 (date, job) 聚合。"""
    cutoff = (_dt.date.today() - _dt.timedelta(days=rows_days)).isoformat()
    q = f"""
        SELECT date, pipeline_name,
               SUM(status='success') s, SUM(status='failed') f,
               SUM(status='skipped') k, COUNT(*) n,
               MAX(CASE WHEN status='failed' THEN error_msg END) err,
               MAX(duration_s) dur, MAX(version) ver
        FROM pipeline_runs
        WHERE date >= ? AND pipeline_name IN ({','.join('?'*len(JOBS))})
        GROUP BY date, pipeline_name
        ORDER BY date DESC, pipeline_name
    """
    with ro_conn() as c:
        raw = c.execute(q, (cutoff, *JOBS)).fetchall()
    out = []
    for d, job, s, f_, k, n, err, dur, ver in raw:
        status = "success" if s else ("failed" if f_ else ("skipped" if k == n else "failed"))
        out.append({
            "date": d, "job": job, "status": status,
            "duration_min": round((dur or 0) / 60, 1),
            "error_brief": (err or "")[:200],
            "version": (ver or "")[:20],
            "generated_at": NOW,
        })
    return out


def e2e_by_date() -> dict[str, str]:
    """e2e jsonl：逐日取最后一条。"""
    out: dict[str, str] = {}
    if not E2E_LOG.exists():
        return out
    for line in E2E_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") != "e2e_verification" or not e.get("date"):
            continue
        total, fails = e.get("total", 0), e.get("fails", 0)
        tag = f"{'pass' if fails == 0 else 'FAIL'} {total - fails}/{total}"
        if e.get("warns"):
            tag += f" ({e['warns']} warn)"
        out[e["date"]] = tag
    return out


def build_t2(rows_days: int):
    """T2：每日运行计数 + 快照字段（open_alerts/overall_health/disk 只填最新日）。"""
    cutoff = (_dt.date.today() - _dt.timedelta(days=rows_days)).isoformat()
    with ro_conn() as c:
        daily = c.execute("""
            SELECT date, COUNT(*), SUM(status='success'), SUM(status='failed'),
                   SUM(status='skipped')
            FROM pipeline_runs WHERE date >= ? GROUP BY date ORDER BY date
        """, (cutoff,)).fetchall()
        failed_jobs = dict(c.execute("""
            SELECT date, GROUP_CONCAT(DISTINCT pipeline_name)
            FROM pipeline_runs
            WHERE date >= ? AND status='failed' GROUP BY date
        """, (cutoff,)).fetchall())
        fresh_red = dict(c.execute("""
            SELECT date, COUNT(*) FROM freshness_audit
            WHERE date >= ? AND status != 'ok' GROUP BY date
        """, (cutoff,)).fetchall())
        open_alerts = c.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
        row = c.execute(
            "SELECT overall_status FROM health_history "
            "ORDER BY check_time DESC LIMIT 1").fetchone()
        health = row[0] if row else "unknown"
    disk_gb = round(shutil.disk_usage(str(QS)).free / 1024**3, 1)
    e2e = e2e_by_date()
    max_date = max((d for d, *_ in daily), default=None)
    out = []
    for d, n, s, f_, k in daily:
        row = {
            "date": d, "total_runs": n, "success": s, "failed": f_, "skipped": k,
            "failed_jobs": (failed_jobs.get(d) or "")[:200],
            "freshness_red_count": fresh_red.get(d, 0),
            "e2e_status": e2e.get(d, "n/a"),
            "generated_at": NOW,
        }
        if d == max_date:  # 快照字段只对最新日有意义
            row["open_alerts"] = open_alerts
            row["overall_health"] = health
            row["disk_free_gb"] = disk_gb
        out.append(row)
    return out


def build_t3(rows_days: int, existing_ids: set[str]):
    """T3：alerts 增量（alert_id 去重）。"""
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=rows_days)).strftime("%Y-%m-%d %H:%M:%S")
    with ro_conn() as c:
        raw = c.execute("""
            SELECT id, severity, layer, title, detail, action,
                   status, resolved_at, created_at
            FROM alerts WHERE created_at >= ? ORDER BY id
        """, (cutoff,)).fetchall()
    out = []
    for aid, sev, layer, title, detail, action, st, rat, cat in raw:
        if str(aid) in existing_ids:
            continue
        out.append({
            "title": title or "", "alert_id": str(aid),
            "date": (cat or "")[:10],
            "severity": sev or "info", "layer": layer or "",
            "detail": (detail or "")[:500], "action": (action or "")[:200],
            "status": st or "open",
            "resolved_at": rat or "", "created_at": cat or "",
        })
    return out


def build_t4(existing_dates: set[str]):
    """T4：stock_pool 最新信号日 pending_entry（行动卡三件齐），JOIN 审批状态。"""
    with ro_conn() as c:
        drow = c.execute("""
            SELECT MAX(added_date) FROM stock_pool
            WHERE status='pending_entry' AND added_date >= date('now','-14 day')
        """).fetchone()
        target = drow[0] if drow else None
        if not target:
            return target, []
        rows = c.execute("""
            SELECT code, name, initial_score, entry_low, entry_high,
                   stop_loss, added_reason, status
            FROM stock_pool
            WHERE status='pending_entry' AND added_date = ?
            ORDER BY initial_score DESC
        """, (target,)).fetchall()
        hrq = dict(c.execute("""
            SELECT code, status FROM human_review_queue hrq
            WHERE created_at = (SELECT MAX(created_at) FROM human_review_queue
                                WHERE code = hrq.code)
        """).fetchall())
    if target in existing_dates:
        return target, []  # 当日已同步，跳过
    out = []
    for i, (code, name, score, lo, hi, sl, reason, st) in enumerate(rows, 1):
        buy = f"{lo}~{hi}" if lo and hi else ""
        out.append({
            "date": target, "code": code, "name": name or "", "rank": i,
            "composite_score": round(score, 3) if score is not None else 0,
            "verdict": st, "buy_range": buy,
            "stop_loss": str(sl) if sl else "",
            "one_line_reason": (reason or "")[:200],
            "review_status": hrq.get(code, "not_queued"),
            "generated_at": NOW,
        })
    return target, out


# ---------- 主流程 ----------

def log_delivery(content_type: str, status: str, error: str, t0: float):
    """铁律 #12：成败留痕（独立读写连接，只写 delivery_log）。

    表结构实测：message_id TEXT NOT NULL（飞书消息 12 位 hex 惯例，
    Bitable 同步无消息 id，用 uuid4 短码占位）；error NOT NULL DEFAULT ''。
    """
    c = None
    try:
        c = sqlite3.connect(str(QS / "data" / "qs.db"), timeout=15)
        c.execute(
            "INSERT INTO delivery_log (message_id, content_type, channel, status, "
            "error, duration_ms, created_at) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12],
             "signal" if content_type == "signal" else content_type,
             "feishu_bitable", status, error[:500] if error else "",
             int((time.time() - t0) * 1000), NOW))
        c.commit()
    except Exception as e:  # 留痕失败也不阻塞，stderr 兜底
        print(f"WARN delivery_log 留痕失败: {e}", file=sys.stderr)
    finally:
        if c:
            c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="回填滚动窗口（天）")
    ap.add_argument("--dry-run", action="store_true", help="只聚合不写 Bitable")
    args = ap.parse_args()

    app = os.getenv("FEISHU_BITABLE_APP_TOKEN")
    tables = {k: os.getenv(v) for k, v in {
        "t1": "FEISHU_BITABLE_DASHBOARD_JOB_TABLE",
        "t2": "FEISHU_BITABLE_DASHBOARD_DIGEST_TABLE",
        "t3": "FEISHU_BITABLE_DASHBOARD_ALERT_TABLE",
        "t4": "FEISHU_BITABLE_SIGNAL_TABLE",
    }.items()}
    missing = [v for k, v in [("app_token", app)] + list(tables.items()) if not v]
    if missing:
        print(f"FAIL 缺 env 配置: {missing}")
        return 1

    t1, t2 = build_t1(args.days), build_t2(args.days)
    token = get_token()
    if not token:
        log_delivery("ops_digest", "failed", "tenant token 获取失败", time.time())
        print("FAIL: tenant token 获取失败")
        return 1

    t3_existing = set()
    for _, f in fetch_all_records(token, app, tables["t3"], ["alert_id"]):
        t3_existing.add(text_of(f.get("alert_id")))
    t3 = build_t3(args.days, t3_existing)

    t4_dates = {text_of(f.get("date")) for _, f in
                fetch_all_records(token, app, tables["t4"], ["date"])}
    t4_date, t4 = build_t4(t4_dates)

    print(f"聚合: T1={len(t1)} T2={len(t2)} T3={len(t3)}(增量) T4={len(t4)}@{t4_date}")
    if args.dry_run:
        for label, rows in (("T1", t1), ("T2", t2), ("T3", t3), ("T4", t4)):
            print(f"--- {label} 样例 ---")
            for r in rows[:3]:
                print(json.dumps(r, ensure_ascii=False))
        return 0

    fails = []

    # T1/T2：目标日期 replace
    for label, table_key, rows, ctype in (
            ("T1", "t1", t1, "ops_job_status"), ("T2", "t2", t2, "ops_digest")):
        t0 = time.time()
        try:
            target_dates = {r["date"] for r in rows}
            old = fetch_all_records(token, app, tables[table_key], ["date"])
            del_ids = [rid for rid, f in old if text_of(f.get("date")) in target_dates]
            if del_ids:
                batch_delete(token, app, tables[table_key], del_ids)
            batch_create(token, app, tables[table_key], rows)
            log_delivery(ctype, "success", "", t0)
            print(f"{label} OK: 写{len(rows)} 删旧{len(del_ids)}")
        except Exception as e:
            log_delivery(ctype, "failed", str(e), t0)
            fails.append(f"{label}: {e}")
            print(f"{label} FAIL: {e}", file=sys.stderr)

    # T3：增量追加
    t0 = time.time()
    try:
        if t3:
            batch_create(token, app, tables["t3"], t3)
        log_delivery("ops_alert", "success", "", t0)
        print(f"T3 OK: 新增{len(t3)}（存量{len(t3_existing)}）")
    except Exception as e:
        log_delivery("ops_alert", "failed", str(e), t0)
        fails.append(f"T3: {e}")
        print(f"T3 FAIL: {e}", file=sys.stderr)

    # T4：最新信号日 replace
    t0 = time.time()
    try:
        if t4:
            old = fetch_all_records(token, app, tables["t4"], ["date"])
            del_ids = [rid for rid, f in old if text_of(f.get("date")) == t4_date]
            if del_ids:
                batch_delete(token, app, tables["t4"], del_ids)
            batch_create(token, app, tables["t4"], t4)
        log_delivery("signal", "success" if t4 else "skipped", "", t0)
        print(f"T4 OK: 写{len(t4)} @{t4_date}")
    except Exception as e:
        log_delivery("signal", "failed", str(e), t0)
        fails.append(f"T4: {e}")
        print(f"T4 FAIL: {e}", file=sys.stderr)

    if fails:
        print(f"PARTIAL: {len(fails)}/4 段失败: {fails}")
        return 2
    print("ALL OK: 4 表同步完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
