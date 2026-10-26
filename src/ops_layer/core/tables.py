"""ops_layer/core/tables.py — Ops Layer DDL"""

OPS_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    error_msg TEXT,
    duration_s REAL,
    version TEXT,
    config_hash TEXT,
    config_hash_effective TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pipeline_date ON pipeline_runs(date, pipeline_name);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
    layer TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    action TEXT,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolved_at TEXT,
    last_escalated_at TEXT,
    last_seen TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT,
    status TEXT,
    detail TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS metric_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL UNIQUE,
    baseline_value REAL,
    baseline_std REAL,
    sample_count INTEGER,
    metric_kind TEXT DEFAULT 'pipeline_duration',
    needs_reset INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS version_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_version TEXT NOT NULL,
    code_branch TEXT,
    config_hash TEXT,
    data_version TEXT,
    deployed_at TEXT NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS health_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_time TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    layer_statuses TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_health_history_time ON health_history(check_time);
"""

def _ensure_metric_baselines_columns(conn) -> None:
    """#80: 老生产库 metric_baselines 幂等补列 (DL-43 先例形态)。

    现表 5 列 (baseline_value/needs_reset/updated_at/created_at + id/metric_name)。
    #80 通电需要 baseline_std/sample_count/metric_kind 三列; PRAGMA 探测缺啥补啥,
    duplicate column 异常吞掉 (幂等双跑安全), 生产 ALTER 属窗口件统筹亲执。
    """
    import sqlite3
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(metric_baselines)").fetchall()}
        if not cols:
            return  # 表不存在 (新库走 DDL 全列, 不在此建表)
        if "baseline_std" not in cols:
            conn.execute("ALTER TABLE metric_baselines ADD COLUMN baseline_std REAL")
        if "sample_count" not in cols:
            conn.execute("ALTER TABLE metric_baselines ADD COLUMN sample_count INTEGER")
        if "metric_kind" not in cols:
            conn.execute("ALTER TABLE metric_baselines ADD COLUMN metric_kind TEXT DEFAULT 'pipeline_duration'")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
