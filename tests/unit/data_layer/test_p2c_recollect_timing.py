"""P2-c: 补采轮后置表的审计时点错位 — 判据执行时点 ≠ 数据到达时点 (判例 #11)。

病灶实证 (生产 qs.db freshness_audit 逐日史):
  10-27 / 10-30 / 11-02 / 11-03 四天各有**同样这 5 张表**被判 missing
  (limit_up_ladder / limit_break_pool / hot_stock_history / skyrocket_list /
  stock_anomaly_reasons), 而 shortterm_recollect 每日 16:40 success 补采,
  freshness_audit 16:22 就跑了 —— 判早了。
  复核: 用生产同一份代码对同一份库重跑 check_collect_freshness("2026-11-02"),
  这 5 张表全部 fresh=True (355/28/30/30/630 行)。⇒ 不是没数据, 是判早了。

修法 (三态, 不假绿也不假红):
  - 有行            → ok
  - 无行 + 有补采轮 → delayed_by_recollect (显式 pending, 不当日告警)
  - 无行 + 无补采轮 → missing (原判不变)
  补采轮跑完后 reconcile_recollect_backed 收敛为 ok / missing。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer.health.readiness import DataReadiness
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
from tests.unit._mutation import assert_mutation_caught

HISTORICAL_DATE = "2026-11-02"
RECOLLECT_TABLES = ["limit_up_ladder", "limit_break_pool", "hot_stock_history",
                    "skyrocket_list", "stock_anomaly_reasons"]


def _today():
    from common.utils import bj_date
    return bj_date()


@pytest.fixture
def before_recollect(monkeypatch):
    """把补采窗起点推到 23:59, 模拟「当前处在补采轮之前」的审计时点。"""
    monkeypatch.setattr(DataReadiness, "RECOLLECT_WINDOW_START", "23:59")


def _mk_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE daily_klines (date TEXT, code TEXT, open REAL, high REAL,
            low REAL, close REAL, volume REAL, amount REAL, UNIQUE(date, code));
        CREATE TABLE stock_info (code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
        CREATE TABLE stock_financials (date TEXT, code TEXT, announce_date TEXT);
        CREATE TABLE stock_valuation (date TEXT, code TEXT);
        CREATE TABLE index_daily (date TEXT, code TEXT);
        CREATE TABLE lhb_daily (date TEXT, code TEXT);
        CREATE TABLE sector_fund_flow (date TEXT, code TEXT);
        CREATE TABLE auction_benchmark (date TEXT, code TEXT);
        CREATE TABLE auction_snapshot (date TEXT, code TEXT);
        CREATE TABLE limit_up_ladder (snapshot_date TEXT, code TEXT);
        CREATE TABLE limit_break_pool (date TEXT, code TEXT);
        CREATE TABLE hot_stock_history (date TEXT, code TEXT);
        CREATE TABLE skyrocket_list (date TEXT, code TEXT);
        CREATE TABLE stock_anomaly_reasons (date TEXT, code TEXT);
    """)
    conn.commit()
    return db


class TestPendingRecollectState:
    def test_recollect_backed_table_is_pending_not_missing(self, tmp_path, before_recollect):
        """补采轮后置表当日无行 → delayed_by_recollect, 不是 missing。"""
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        rows = db.get_connection().execute(
            "SELECT table_name, status FROM freshness_audit WHERE date=? "
            "AND table_name IN (%s)" % ",".join("?" * len(RECOLLECT_TABLES)),
            (_today(), *RECOLLECT_TABLES)).fetchall()
        got = {r[0]: r[1] for r in rows}
        assert len(got) == 5
        for t in RECOLLECT_TABLES:
            assert got[t] == "delayed_by_recollect", f"{t} 应为 pending 态, 实为 {got[t]}"

    def test_pending_does_not_trigger_missing_alert(self, tmp_path, before_recollect):
        """pending 态不入告警链 (告警只认 missing) —— 补采轮 5 表不再每天假阳。

        注: 本夹具只建了补采轮 5 表 + 少量辅助表, 其余表(如 stock_info)缺失
        属夹具限制, 故只断言这 5 张表**不在 missing 集合**里。
        """
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        rows = db.get_connection().execute(
            "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
            (_today(),)).fetchall()
        missing = {r[0] for r in rows}
        leaked = missing & set(RECOLLECT_TABLES)
        assert not leaked, f"补采轮后置表被误判 missing: {sorted(leaked)}"

    def test_non_recollect_table_still_missing(self, tmp_path):
        """不在补采轮册的表保持原判 (双向: 不冤枉无辜, 真缺仍显形)。"""
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        row = db.get_connection().execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name='sector_fund_flow'",
            (_today(),)).fetchone()
        assert row is not None and row[0] == "missing"

    def test_table_with_data_is_ok(self, tmp_path):
        """有行 → ok (补采轮册不影响已有数据的判定)。"""
        db = _mk_db(tmp_path)
        conn = db.get_connection()
        conn.execute("INSERT INTO hot_stock_history (date, code) VALUES (?, '600000')", (_today(),))
        conn.commit()
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        row = conn.execute(
            "SELECT status FROM freshness_audit WHERE date=? AND table_name='hot_stock_history'",
            (_today(),)).fetchone()
        assert row[0] == "ok"


class TestReconcile:
    def test_reconcile_to_ok_when_recollect_filled(self, tmp_path, before_recollect):
        """补采轮补上数据后, 二次核验收敛为 ok。"""
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        # 模拟 16:40 补采写入
        conn = db.get_connection()
        for t in RECOLLECT_TABLES:
            col = "snapshot_date" if t == "limit_up_ladder" else "date"
            conn.execute(f"INSERT INTO {t} ({col}, code) VALUES (?, '600000')", (_today(),))
        conn.commit()
        rec = pipe.reconcile_recollect_backed(_today())
        assert rec["checked"] == 5 and rec["ok"] == 5 and rec["missing"] == 0
        left = conn.execute(
            "SELECT COUNT(*) FROM freshness_audit WHERE date=? AND status='delayed_by_recollect'",
            (_today(),)).fetchone()[0]
        assert left == 0

    def test_reconcile_to_missing_when_recollect_also_empty(self, tmp_path, before_recollect):
        """补采也拿不到 → 转 missing (真断供必须显形, 不假绿)。"""
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        rec = pipe.reconcile_recollect_backed(_today())
        assert rec["checked"] == 5 and rec["missing"] == 5 and rec["ok"] == 0
        rows = db.get_connection().execute(
            "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
            (_today(),)).fetchall()
        assert {r[0] for r in rows} >= set(RECOLLECT_TABLES)

    def test_reconcile_idempotent(self, tmp_path, before_recollect):
        """二次核验幂等 (重复跑不改变终态)。"""
        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        pipe.reconcile_recollect_backed(_today())
        conn = db.get_connection()
        before = {r[0] for r in conn.execute(
            "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
            (_today(),)).fetchall()}
        rec2 = pipe.reconcile_recollect_backed(_today())
        after = {r[0] for r in conn.execute(
            "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
            (_today(),)).fetchall()}
        assert rec2["checked"] == 0  # 无 pending 行可收敛
        assert before == after


class TestReadinessContract:
    def test_check_collect_freshness_reports_pending(self, tmp_path, before_recollect):
        """readiness 层直接暴露 pending_recollect 标记 (下游唯一事实源)。"""
        db = _mk_db(tmp_path)
        out = DataReadiness(db).check_collect_freshness(_today())
        assert out["hot_stock_history"].get("pending_recollect") == "shortterm_recollect"
        assert out["sector_fund_flow"]["fresh"] is False
        assert "pending_recollect" not in out["sector_fund_flow"]

    def test_mutation_pending_would_be_missing(self, tmp_path, before_recollect):
        """变异验证: 若补采轮册为空 (退回旧口径), 这 5 表会全判 missing。

        这是**真实**变异而非形式变异: 直接清空 RECOLLECT_BACKED_TABLES 再跑
        同一管线, 断言 missing 计数会从 0 变成 5 —— 证明该注册表是唯一
        分水岭, 去掉它判据立刻退回每天假阳。
        """
        from data_layer.health import readiness as _r

        db = _mk_db(tmp_path)
        pipe = FreshnessAuditPipeline(db)
        pipe.run(PipelineInput(date=_today(), db=db))
        conn = db.get_connection()
        leaked = {r[0] for r in conn.execute(
            "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
            (_today(),)).fetchall()} & set(RECOLLECT_TABLES)
        assert not leaked

        saved = dict(_r.DataReadiness.RECOLLECT_BACKED_TABLES)

        def _mutate():
            _r.DataReadiness.RECOLLECT_BACKED_TABLES = {}

        def _check():
            sub = tmp_path / "m"
            sub.mkdir(exist_ok=True)
            db2 = _mk_db(sub)
            FreshnessAuditPipeline(db2).run(PipelineInput(date=_today(), db=db2))
            bad = {r[0] for r in db2.get_connection().execute(
                "SELECT table_name FROM freshness_audit WHERE date=? AND status='missing'",
                (_today(),)).fetchall()} & set(RECOLLECT_TABLES)
            assert not bad, f"清空补采轮册后 {sorted(bad)} 被判 missing (口径回退)"

        try:
            assert_mutation_caught(mutate=_mutate, check=_check,
                                   what="补采轮册 (pending 态)")
        finally:
            _r.DataReadiness.RECOLLECT_BACKED_TABLES = saved
