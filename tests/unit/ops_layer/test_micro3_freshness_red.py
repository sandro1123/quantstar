"""哨兵④ RED: freshness_audit 加 stock_financials 公告新鲜度规则

语义病灶: 现规则按 date 列 (报告期末日) 判 freshness → stock_financials
date 恒=季末 → 永续 delayed 95,040min 假警报 (与 08-24 体检旧账同族)。
修法: 新规则键 stock_financials_announce, 按 MAX(announce_date):
  - 财报季 (fiscal_season_months 可配置): 停滞 > in_season_stale_days → delayed
  - 非财报季: > off_season_stale_days → delayed
阈值入 config/defaults/ops.yaml freshness_audit 专段独立块。

披露历 (A 股): 财报季月=[1,4,7,8,10] (年报预告 1 月/年报+一季报 4 月/
半年报 7-8 月/三季报 10 月); 2/3/5/6/9/11/12 为间歇期。
GREEN 期全部应过。
"""
import sqlite3

import pytest

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline


@pytest.fixture()
def db():
    d = Database(":memory:")
    d.init_db()
    conn = d.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_financials (
            code TEXT, date TEXT, eps REAL, announce_date TEXT);
        CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL);
        CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT);
        CREATE TABLE IF NOT EXISTS stock_valuation (code TEXT, date TEXT, pe REAL);
    """)
    conn.commit()
    yield d
    conn.close()


def _rows(conn, audit_date):
    return {r[0]: r for r in conn.execute(
        "SELECT table_name, actual_at, delay_min, status FROM freshness_audit "
        "WHERE date=?", (audit_date,))}


class TestAnnounceFreshnessRule:
    def test_new_rule_key_present(self, db):
        """审计后必须出现独立键 stock_financials_announce (与旧 date 规则行不混)。"""
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-10-09", db=db))
        conn = db.get_connection()
        keys = {r[0] for r in conn.execute(
            "SELECT table_name FROM freshness_audit WHERE date='2026-10-09'")}
        assert "stock_financials_announce" in keys

    def test_fresh_announce_ok_in_season(self, db):
        """财报季 (10 月=三季报披露月) 且公告新鲜 → ok。"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-06-29',0.5,'2026-10-07')")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-10-09", db=db))
        rows = _rows(conn, "2026-10-09")
        row = rows["stock_financials_announce"]
        assert row[3] == "ok", f"财报季 2 天内公告应为 ok, got {row}"

    def test_stale_announce_delayed_in_season(self, db):
        """财报季公告停滞 >3 自然日 → delayed (10 月审计日 vs 9 月尾公告)。"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-06-29',0.5,'2026-09-28')")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-10-09", db=db))
        rows = _rows(conn, "2026-10-09")
        row = rows["stock_financials_announce"]
        assert row[3] == "delayed", f"财报季停滞 11 天应为 delayed, got {row}"
        assert row[1] == "2026-09-28", "actual_at 应为 MAX(announce_date)"

    def test_off_season_fresh_ok(self, db):
        """非财报季 (如 12 月) 公告停滞在披露窗内 → ok。"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-09-29',0.5,'2026-11-28')")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-12-10", db=db))
        rows = _rows(conn, "2026-12-10")
        assert rows["stock_financials_announce"][3] == "ok", \
            "非财报季 12 天停滞 (<45) 应为 ok"

    def test_off_season_very_stale_delayed(self, db):
        """非财报季公告停滞 >45 天 → delayed。"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-09-29',0.5,'2026-10-15')")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-12-10", db=db))
        rows = _rows(conn, "2026-12-10")
        assert rows["stock_financials_announce"][3] == "delayed", \
            "非财报季 56 天停滞 (>45) 应为 delayed"

    def test_no_announce_data_missing(self, db):
        """announce_date 全 NULL/空表 → missing, 不是 crash。"""
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-06-29',0.5,NULL)")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        p.run(PipelineInput(date="2026-10-09", db=db))
        rows = _rows(conn, "2026-10-09")
        assert rows["stock_financials_announce"][3] == "missing"


class TestAnnounceConfig:
    def test_thresholds_configurable(self, db):
        """阈值/财报季月份可注入覆盖 — 双向证明 off_season 阈值真被消费。

        同一份数据 (10 月审计日 vs 9 月尾公告, 停滞 11 天), months=[] 强制
        走非财报季腿: off=30 → 11<30 ok; off=5 → 11>5 delayed。
        两场景只有 off_season_stale_days 不同, 结果必随其翻转。
        (yaml 加载腿的通电证明在 test_yaml_config_loaded, 此处只证消费。)
        """
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-06-29',0.5,'2026-09-28')")
        conn.commit()
        # 场景A: off_season 阈值 30 天 → 停滞 11 < 30 → ok
        p = FreshnessAuditPipeline(db=db)
        p.announce_cfg = {"fiscal_season_months": [], "in_season_stale_days": 3,
                          "off_season_stale_days": 30}
        p.run(PipelineInput(date="2026-10-09", db=db))
        rows = _rows(conn, "2026-10-09")
        assert rows["stock_financials_announce"][3] == "ok", \
            "off=30 时停滞 11 天应 ok — 证明 off_season_stale_days 真被消费"

        # 场景B: 同数据 off=5 → 11>5 → delayed
        conn.execute("DELETE FROM freshness_audit WHERE date='2026-10-09'")
        conn.commit()
        p2 = FreshnessAuditPipeline(db=db)
        p2.announce_cfg = {"fiscal_season_months": [], "in_season_stale_days": 3,
                           "off_season_stale_days": 5}
        p2.run(PipelineInput(date="2026-10-09", db=db))
        rows = _rows(conn, "2026-10-09")
        assert rows["stock_financials_announce"][3] == "delayed", \
            "off=5 时停滞 11 天应 delayed — 双向证明阈值真被消费"

    def test_yaml_config_loaded(self, db):
        """yaml 通电证明锚 — 默认值与生产值故意分叉, 唯一翻转键=8 月。

        默认 fiscal_season_months=[1,4,7,10] (无 8 月), ops.yaml 生产值
        [1,4,7,8,10] (含 8 月中报窗)。同数据 (8 月审计日, 停滞 11 天):
          - 若 _load_announce_cfg 消费了 yaml → 8 月=财报季, in=3 → delayed
          - 若 loader 断线回落默认 → 8 月=非财报季, off=45 → ok
        本测试断言真消费形态。首版病灶: common.config 不存在 + 段路径缺
        ops.pipelines 两层 + except 静默 → 永回落默认 (probe37~41 取证)。
        """
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_financials VALUES ('000001','2026-06-29',0.5,'2026-08-01')")
        conn.commit()
        p = FreshnessAuditPipeline(db=db)
        # 8 月在 yaml 生产月列表内 (不在默认) — 分叉锚
        assert 8 in p.announce_cfg["fiscal_season_months"], (
            "yaml 生产值 [1,4,7,8,10] 未被消费 — loader 断线回落默认 "
            "[1,4,7,10]: %r" % (p.announce_cfg,))
        p.run(PipelineInput(date="2026-08-12", db=db))
        rows = _rows(conn, "2026-08-12")
        assert rows["stock_financials_announce"][3] == "delayed", \
            "8 月财报季停滞 11 天 (>3) 应 delayed — yaml 通电判 delayed"
