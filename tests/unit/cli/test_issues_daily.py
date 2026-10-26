"""issues daily — 每日问题报告 (只读, 批 2: 11 源全接入 + stale-alerts 旧账清单)

只读 + --json 稳定 schema + 中文化。红绿测试 (复用 advisory 测试模式)。
"""
import json
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from cli.main import cli

SCHEMA = """
CREATE TABLE pipeline_runs (run_id TEXT, pipeline_name TEXT, date TEXT,
    status TEXT, error_msg TEXT, created_at TEXT);
CREATE TABLE alerts (id INTEGER, severity TEXT, layer TEXT, title TEXT,
    detail TEXT, status TEXT, created_at TEXT);
CREATE TABLE data_quality_flags (code TEXT, date TEXT, table_name TEXT,
    field TEXT, flag TEXT, reason TEXT, rule TEXT, created_at TEXT);
CREATE TABLE freshness_audit (date TEXT, table_name TEXT, expected_by TEXT,
    actual_at TEXT, delay_min INTEGER, status TEXT, created_at TEXT);
CREATE TABLE quality_checks (date TEXT, table_name TEXT, check_name TEXT,
    level TEXT, result TEXT, detail TEXT, created_at TEXT);
CREATE TABLE health_history (id INTEGER, check_time TEXT, overall_status TEXT,
    layer_statuses TEXT, created_at TEXT);
CREATE TABLE delivery_log (id INTEGER, message_id TEXT, content_type TEXT,
    channel TEXT, status TEXT, error TEXT, duration_ms INTEGER, created_at TEXT);
CREATE TABLE human_review_queue (id INTEGER, code TEXT, decision_type TEXT,
    reason TEXT, score REAL, confidence REAL, status TEXT, created_at TEXT,
    reviewed_at TEXT, reviewer TEXT, review_note TEXT, entry_low REAL,
    entry_high REAL, stop_loss REAL, take_profit REAL, consumed INTEGER,
    message_id TEXT);
"""

@pytest.fixture
def issues_db(tmp_path, monkeypatch):
    """临时只读库 + 全源种子; issues 的 db/logs 路径解析指向它"""
    db_path = str(tmp_path / "issues.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?)", [
            ("r1", "factor_update", "2026-08-28", "failed",
             "database is closed", "2026-08-28 16:05:00"),
            ("r2", "daily_backtest", "2026-08-28", "partial",
             None, "2026-08-28 16:35:00"),
            ("r3", "morning_review", "2026-08-28", "success",
             None, "2026-08-28 09:00:00"),
            ("r4", "factor_update", "2026-08-27", "failed",
             "old failure", "2026-08-27 16:05:00"),
            # intraday_scan: 08-21 告警后全 success (stale 证据种子)
            ("r5", "intraday_scan", "2026-08-28", "success",
             None, "2026-08-28 10:00:00"),
            ("r6", "intraday_scan", "2026-08-28", "success",
             None, "2026-08-28 10:05:00"),
        ])
    conn.executemany(
        "INSERT INTO alerts VALUES (?,?,?,?,?,?,?)", [
            # 当日告警须动态今日 — 硬编码日期会日历腐烂
            # (2026-08-28 写成时=当日, 两周后腐成"旧账"误入选, 09-11 实证)
            (1, "critical", "data", "factor_daily 停滞", "3 天无新数据",
             "open", datetime.now().strftime("%Y-%m-%d") + " 10:00:00"),
            (2, "warning", "ops", "磁盘 80%", None,
             "open", datetime.now().strftime("%Y-%m-%d") + " 11:00:00"),
            (3, "critical", "execution", "已解决告警", "x",
             "resolved", "2026-08-28 09:00:00"),
            # 旧账: 08-21 intraday_scan 告警 (其后全 success → 可 resolve)
            (27, "critical", "ops", "直连脚本异常: intraday_scan", "traceback...",
             "open", "2026-08-21 09:30:01"),
        ])
    conn.executemany(
        "INSERT INTO data_quality_flags VALUES (?,?,?,?,?,?,?,?)", [
            ("000001", "2026-08-28", "daily_klines", "close", "mad_outlier",
             "x", "mad_outlier", "2026-08-28 08:00:00"),
            ("000002", "2026-08-28", "daily_klines", "close", "mad_outlier",
             "x", "mad_outlier", "2026-08-28 08:00:01"),
            # 7 组种子 → top-5 + 折叠 1 条 (闸①测试)
            ("c1", "2026-08-28", "t1", "f", "r1", "x", "r1", "2026-08-28 08:01:00"),
            ("c2", "2026-08-28", "t2", "f", "r2", "x", "r2", "2026-08-28 08:02:00"),
            ("c3", "2026-08-28", "t3", "f", "r3", "x", "r3", "2026-08-28 08:03:00"),
            ("c4", "2026-08-28", "t4", "f", "r4", "x", "r4", "2026-08-28 08:04:00"),
            ("c5", "2026-08-28", "t5", "f", "r5", "x", "r5", "2026-08-28 08:05:00"),
            ("c6", "2026-08-28", "t6", "f", "r6", "x", "r6", "2026-08-28 08:06:00"),
            # 昨日已有 r1 → 今日 r1 非新增, r2~r6 新增
            ("c1", "2026-08-27", "t1", "f", "r1", "x", "r1", "2026-08-27 08:01:00"),
        ])
    conn.execute("INSERT INTO freshness_audit VALUES "
                 "('2026-08-28','stock_financials','08:00','09:30',90,'delayed',"
                 "'2026-08-28 09:31:00')")
    conn.execute("INSERT INTO quality_checks VALUES "
                 "('2026-08-28','factor_daily','row_count','error','fail',"
                 "'行数 0 低于阈值','2026-08-28 08:30:00')")
    conn.execute("INSERT INTO health_history VALUES "
                 "(1,'2026-08-28 17:00','red',"
                 "'{\"data\": \"red\", \"agent\": \"green\"}','2026-08-28 17:00')")
    conn.execute("INSERT INTO delivery_log VALUES "
                 "(1,'m1','morning_brief','feishu','failed','token expired',500,"
                 "'2026-08-28 09:01:00')")
    conn.execute("INSERT INTO human_review_queue (id, code, status, created_at) "
                 "VALUES (1,'000001','pending', datetime('now','-48 hours'))")
    conn.commit()
    conn.close()
    # 文件源种子: e2e jsonl + watchdog log
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "e2e_verification.jsonl").write_text(
        json.dumps({"event": "e2e_verification", "phase": "full",
                    "date": "2026-08-28", "total": 15, "fails": 1, "warns": 0,
                    "checks": [{"name": "evening_report", "status": "fail",
                                "detail": "status=failed"}],
                    "timestamp": "2026-08-28T18:00:00"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (logs / "pipeline.log").write_text(
        "2026-08-28 16:01:00 [flock_guard] WARNING skipped_lock key=paper_trading "
        "lock_owner=daily_data_pipeline(pid=1) held=3900s\n"
        "2026-08-28 16:02:00 [flock_guard] WARNING skipped_lock key=paper_trading "
        "lock_owner=daily_data_pipeline(pid=1) held=3960s\n"
        # mass-skip 种子: 16:30 窗口 5 个不同键 (08-26 事故模式)
        "2026-08-28 16:31:00 [flock_guard] WARNING skipped_lock key=evening_report "
        "lock_owner=daily_data_pipeline(pid=1) held=3900s\n"
        "2026-08-28 16:32:00 [flock_guard] WARNING skipped_lock key=factor_update "
        "lock_owner=daily_data_pipeline(pid=1) held=3960s\n"
        "2026-08-28 16:33:00 [flock_guard] WARNING skipped_lock key=morning_review "
        "lock_owner=daily_data_pipeline(pid=1) held=4020s\n"
        "2026-08-28 16:34:00 [flock_guard] WARNING skipped_lock key=ic_feedback "
        "lock_owner=daily_data_pipeline(pid=1) held=4080s\n"
        "2026-08-28 16:35:00 [flock_guard] WARNING skipped_lock key=daily_backtest "
        "lock_owner=daily_data_pipeline(pid=1) held=4140s\n"
        "2026-08-28 17:01:00 [cron_watchdog] KILLED key=daily_data_pipeline "
        "budget=5550s rc=124 (SIGTERM->SIGKILL)\n"
        "2026-08-27 16:01:00 [flock_guard] WARNING skipped_lock key=old_day\n",
        encoding="utf-8")
    import cli.issues as mod
    monkeypatch.setattr(mod, "_resolve_db_path", lambda: db_path)
    monkeypatch.setattr(mod, "_resolve_logs_dir", lambda: str(logs))
    monkeypatch.setattr(mod, "_today", lambda: "2026-08-28")
    return db_path

def _invoke(args):
    return CliRunner().invoke(cli, ["--json"] + args)

def _json(result):
    return json.loads(result.output)

class TestIssuesDaily:
    def test_collects_failed_and_partial(self, issues_db):
        r = _invoke(["issues", "daily"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["schema_version"] == 1
        s1 = [i for i in d["issues"] if i["source"] == "pipeline_runs"]
        assert len(s1) == 2
        assert {i["severity"] for i in s1} == {"critical", "warning"}

    def test_all_sources_present(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        sources = {i["source"] for i in d["issues"]}
        assert {"pipeline_runs", "alerts", "data_quality_flags", "freshness_audit",
                "quality_checks", "health_history", "delivery_log",
                "e2e_verification", "watchdog",
                "human_review_queue"} <= sources

    def test_s3_top5_and_fold(self, issues_db):
        """闸①: 7 组 → top-5 + 折叠 1 条; 新增 rule 标记"""
        d = _json(_invoke(["issues", "daily"]))
        s3 = [i for i in d["issues"] if i["source"] == "data_quality_flags"]
        folded = [i for i in s3 if "rest_folded" in i["dedup_key"]]
        top = [i for i in s3 if "rest_folded" not in i["dedup_key"]]
        assert len(top) == 5
        assert len(folded) == 1
        assert folded[0]["severity"] == "info"
        assert folded[0]["evidence"]["total_count"] == 2  # 折叠 2 组各 1 条
        # mad_outlier ×2 排 top1
        assert "×2" in top[0]["title_zh"]
        # 新增 rule 标记: mad_outlier 昨日没有 → [新增]
        assert any("[新增]" in i["title_zh"] for i in top)
        # r1 昨日已有 → 若入 top5 不带 [新增]
        r1 = [i for i in top if ":r1:" in i["dedup_key"]]
        for i in r1:
            assert "[新增]" not in i["title_zh"]

    def test_s6_health_red_layers(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        s6 = [i for i in d["issues"] if i["source"] == "health_history"]
        assert len(s6) == 1
        assert s6[0]["severity"] == "critical"
        assert s6[0]["evidence"]["bad_layers"] == {"data": "red"}

    def test_s8_e2e_fail(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        s8 = [i for i in d["issues"] if i["source"] == "e2e_verification"]
        assert len(s8) == 1
        assert s8[0]["severity"] == "critical"

    def test_s9_mass_skip_folded(self, issues_db):
        """闸③: 16:30 窗口 5 键 → 折叠成一条 critical mass-skip, 不逐键刷屏"""
        d = _json(_invoke(["issues", "daily"]))
        s9 = [i for i in d["issues"] if i["source"] == "watchdog"]
        mass = [i for i in s9 if "mass_skip" in i["dedup_key"]]
        assert len(mass) == 1
        assert mass[0]["severity"] == "critical"
        assert mass[0]["evidence"]["total"] == 5
        # mass-skip 的键不再逐条出现
        for k in ("evening_report", "factor_update", "morning_review",
                  "ic_feedback", "daily_backtest"):
            assert not any(f"skipped_lock:{k}:" in i["dedup_key"] for i in s9)
        # 非 mass 的 paper_trading 仍逐条 warning
        pt = next(i for i in s9 if "skipped_lock:paper_trading" in i["dedup_key"])
        assert pt["severity"] == "warning"
        assert pt["evidence"]["count"] == 2
        # KILLED 仍逐条 critical; 昨日行不收
        assert any("killed" in i["dedup_key"] for i in s9)
        assert not any("old_day" in i["dedup_key"] for i in s9)

    def test_s2_zombie_folded(self, issues_db):
        """闸②: 08-21 intraday_scan 旧账 healthy=True → 折叠成一条 info, 不逐条刷屏"""
        d = _json(_invoke(["issues", "daily"]))
        s2 = [i for i in d["issues"] if i["source"] == "alerts"]
        zombie = [i for i in s2 if "zombie_batch" in i["dedup_key"]]
        assert len(zombie) == 1
        assert zombie[0]["severity"] == "info"
        assert 27 in zombie[0]["evidence"]["alert_ids"]
        # 僵尸不再逐条出现
        assert not any("alert27" in i["dedup_key"] for i in s2)
        # 证据不足/仍异常的当日告警仍逐条报
        assert any("alert1" in i["dedup_key"] for i in s2)
        assert any("alert2" in i["dedup_key"] for i in s2)

    def test_s11_hrq_backlog(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        s11 = [i for i in d["issues"] if i["source"] == "human_review_queue"]
        assert len(s11) == 1
        assert s11[0]["evidence"]["count"] == 1

    def test_dedup_key_unique(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        keys = [i["dedup_key"] for i in d["issues"]]
        assert len(keys) == len(set(keys))

    def test_closed_database_rule_hit(self, issues_db):
        d = _json(_invoke(["issues", "daily"]))
        fu = next(i for i in d["issues"] if "factor_update" in i["dedup_key"])
        assert "closed-database" in fu["suggested_action_zh"]

    def test_bad_date_exit_2(self, issues_db):
        r = _invoke(["issues", "daily", "--date", "20260828"])
        assert r.exit_code == 2
        assert _json(r)["error"]["code"] == "BAD_DATE"

    def test_missing_table_degrades_not_crash(self, tmp_path, monkeypatch):
        """单源表缺失 → 降级为 warning issue, 不拖垮全报告 (铁律#12)"""
        db_path = str(tmp_path / "bare.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE pipeline_runs (run_id TEXT, pipeline_name TEXT, "
                     "date TEXT, status TEXT, error_msg TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        import cli.issues as mod
        monkeypatch.setattr(mod, "_resolve_db_path", lambda: db_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setattr(mod, "_resolve_logs_dir", lambda: str(logs))
        monkeypatch.setattr(mod, "_today", lambda: "2026-08-28")
        r = _invoke(["issues", "daily"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert any(i["dedup_key"].startswith("S0:alerts:collect_error")
                   for i in d["issues"])

    def test_readonly_enforced(self, issues_db):
        """mode=ro 物理只读实证 — 写操作必抛"""
        import cli.issues as mod
        conn = mod._ro_connect()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE hack (x TEXT)")
        conn.close()

    def test_human_output_chinese(self, issues_db):
        r = CliRunner().invoke(cli, ["issues", "daily"])
        assert r.exit_code == 0
        assert "每日问题报告" in r.output
        assert "管道失败" in r.output

    def test_empty_day(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        logs = tmp_path / "logs"
        logs.mkdir()
        import cli.issues as mod
        monkeypatch.setattr(mod, "_resolve_db_path", lambda: db_path)
        # 不 patch 会读到生产 logs/ 真文件 (watchdog.jsonl 等) 误报 — 必须指向空 tmp 目录
        monkeypatch.setattr(mod, "_resolve_logs_dir", lambda: str(logs))
        monkeypatch.setattr(mod, "_today", lambda: "2026-08-28")
        r = _invoke(["issues", "daily"])
        assert r.exit_code == 0  # 无问题 ≠ 命令坏
        d = _json(r)
        assert d["summary"]["total"] == 0

class TestStaleAlerts:
    def test_stale_alert_healthy_evidence(self, issues_db):
        """08-21 intraday_scan 旧账: 其后全 success → healthy=True 建议 resolve"""
        r = _invoke(["issues", "stale-alerts", "--min-age-days", "6"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["command"] == "issues stale-alerts"
        # 只收旧账 (min-age 1 天): alert 27 入选, 当日 alert 1/2 不入
        ids = [e["alert_id"] for e in d["stale_alerts"]]
        assert 27 in ids
        assert 1 not in ids and 2 not in ids
        e27 = next(e for e in d["stale_alerts"] if e["alert_id"] == 27)
        assert e27["health_evidence"]["healthy"] is True
        assert e27["health_evidence"]["success_after"] == 2
        assert e27["health_evidence"]["failed_after"] == 0
        assert "resolve" in e27["recommendation_zh"]

    def test_stale_alert_unknown_pattern(self, issues_db, tmp_path):
        """无匹配模式 → healthy=None 待查, 不编造"""
        conn = sqlite3.connect(issues_db)
        conn.execute("INSERT INTO alerts VALUES "
                     "(99,'warning','data','神秘告警','x','open','2026-08-20 01:00')")
        conn.commit()
        conn.close()
        d = _json(_invoke(["issues", "stale-alerts"]))
        e99 = next(e for e in d["stale_alerts"] if e["alert_id"] == 99)
        assert e99["health_evidence"]["healthy"] is None
        assert "观察" in e99["recommendation_zh"]

    def test_stale_alerts_readonly(self, issues_db):
        """stale-alerts 不写库 — resolve 裁定归统筹"""
        _invoke(["issues", "stale-alerts"])
        conn = sqlite3.connect(issues_db)
        n_open = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
        conn.close()
        assert n_open == 3  # 原样, 未被改动

    def test_stale_alerts_human(self, issues_db):
        r = CliRunner().invoke(cli, ["issues", "stale-alerts"])
        assert r.exit_code == 0
        assert "旧 alert 清单" in r.output

class TestSyncPending:
    def test_sync_writes_to_ledger(self, issues_db, monkeypatch):
        """sync = 聚合 + 台账 upsert; info 级跳过; 结果挂 sync_result"""
        captured = {}

        def fake_sync(issues_list):
            captured["issues"] = issues_list
            return {"ok": True, "created": 5, "updated": 1, "skipped_info": 2,
                    "errors": []}

        import cli.issues_ledger as ledger
        monkeypatch.setattr(ledger, "sync_issues", fake_sync)
        r = _invoke(["issues", "sync"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["command"] == "issues sync"
        assert d["sync_result"]["created"] == 5
        # 聚合结果照常产出
        assert d["summary"]["total"] > 0

    def test_sync_ledger_not_configured_exit0(self, issues_db, monkeypatch):
        """台账未配置 → exit 0 (聚合成功≠命令坏), sync_result 带失败码"""
        import cli.issues_ledger as ledger
        monkeypatch.setattr(ledger, "sync_issues", lambda iss: {
            "ok": False, "code": "LEDGER_NOT_CONFIGURED",
            "message_zh": "issues 台账未配置"})
        r = _invoke(["issues", "sync"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["sync_result"]["ok"] is False
        assert d["sync_result"]["code"] == "LEDGER_NOT_CONFIGURED"

    def test_sync_bad_date_exit_2(self, issues_db):
        r = _invoke(["issues", "sync", "--date", "bad"])
        assert r.exit_code == 2

    def test_pending_reads_ledger(self, issues_db, monkeypatch):
        import cli.issues_ledger as ledger
        monkeypatch.setattr(ledger, "read_pending", lambda: {
            "ok": True, "total": 1, "pending": [{
                "record_id": "rec1", "dedup_key": "S1:x:failed:20260828",
                "title_zh": "x 管道失败", "source": "pipeline_runs",
                "severity": "critical", "status": "pending",
                "first_seen": "2026-08-28", "occurrences_7d": 1}]})
        r = _invoke(["issues", "pending"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["summary"]["total"] == 1
        assert d["pending"][0]["dedup_key"] == "S1:x:failed:20260828"

    def test_pending_ledger_error_exit_1(self, issues_db, monkeypatch):
        import cli.issues_ledger as ledger
        monkeypatch.setattr(ledger, "read_pending", lambda: {
            "ok": False, "code": "LEDGER_NOT_CONFIGURED",
            "message_zh": "未配置"})
        r = _invoke(["issues", "pending"])
        assert r.exit_code == 1
        assert _json(r)["error"]["code"] == "LEDGER_NOT_CONFIGURED"

class TestLedgerHelpers:
    def test_text_of_segmented_array(self):
        """Bitable Text 列分段数组解包 (08-27 仪表盘坑入账)"""
        from cli.issues_ledger import text_of
        assert text_of([{"text": "S1:x:"}, {"text": "failed"}]) == "S1:x:failed"
        assert text_of("plain") == "plain"
        assert text_of(None) == ""
        assert text_of(42) == "42"

    def test_sync_severities_exclude_info(self):
        """info 级折叠汇总不入台账 — 防噪音"""
        from cli.issues_ledger import SYNC_SEVERITIES
        assert "info" not in SYNC_SEVERITIES
        assert "critical" in SYNC_SEVERITIES and "warning" in SYNC_SEVERITIES
