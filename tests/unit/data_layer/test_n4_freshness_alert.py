# -*- coding: utf-8 -*-
"""N4 甲-1 RED — freshness 判出的 missing 必须进告警链。

病根（N4 侦察账实证）: `freshness_audit` 在 09-07/08/09/14 都把 lhb_daily 判成
`missing` —— **判定正确、落库正确**。但看那几天的告警:
    09-08 当天 **0 条告警**; 09-07/09/14 的全是 Ghost 线程/依赖饿死,
    **没有任何一条是「某张表 missing」**。
→ **有判定, 没广播。** 与 AUD-022(执行层结算面告警零覆盖)同一形状:
  机制建好了、判得对、落了库, **但没接到告警链上, 于是等于没判**。
  这是「建了没通电」在**审计面与告警面之间**的一例。

设计两条硬约束:
1. **一天一条, 不逐表轰炸** —— 表名进 detail, 不进 title。
   title 必须**跨 N 稳定**, 否则 AlertManager 的同名 open 抑制失效, 每天一条新 open,
   积压面从 12 条涨成天天涨 (AUD-019 的教训: 告警没人收 = 告警等于没有,
   而刷屏是"没人收"最快的成因)。
2. **fail-safe 但不静默** —— 告警失败只 warning 不阻断审计, 但必须留痕
   (同 AUD-005 `_alert_settlement_deferred` 的既定样板)。
"""
import sqlite3

import pytest
from common.database import Database

MISS = "2026-09-08"


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_db()
    conn = d.get_connection()
    # 夹具须覆盖被测面真正会写的表 —— 首版只建了 freshness_audit,
    # AlertManager 写 alerts 时 no such table (我自己又犯了一次「夹具窄面」)。
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, severity TEXT NOT NULL,
            layer TEXT, title TEXT NOT NULL, detail TEXT, action TEXT,
            status TEXT DEFAULT 'open', resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now','+8 hours')));
        CREATE TABLE IF NOT EXISTS freshness_audit (
            date TEXT NOT NULL, table_name TEXT NOT NULL, expected_by TEXT,
            actual_at TEXT, delay_min INTEGER, status TEXT NOT NULL DEFAULT 'missing',
            created_at TEXT DEFAULT (datetime('now','+8 hours')),
            PRIMARY KEY (date, table_name));
    """)
    # v8 后 AlertManager INSERT 带 last_seen/last_escalated_at; 夹具的旧版
    # alerts 表必须走真实迁移路径补列 (同 st31 判例: 夹具比生产宽/旧 = 病根镜像)。
    from execution_layer.migrations import ensure_schema
    ensure_schema(d)
    conn.commit()
    yield d
    d.close()


def _seed(db, date, rows):
    conn = db.get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO freshness_audit "
        "(date, table_name, expected_by, actual_at, delay_min, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(date, t, date, None if s == "missing" else date, None, s) for t, s in rows])
    conn.commit()


def _pipeline(db):
    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
    return FreshnessAuditPipeline(db=db)


def _alerts(db):
    return db.get_connection().execute(
        "SELECT severity, title, detail FROM alerts ORDER BY id").fetchall()


# ------------------------------------------------------------ 广播面

def test_missing_tables_produce_exactly_one_alert(db):
    """3 张表 missing → **1 条**告警, 不是 3 条。"""
    _seed(db, MISS, [("lhb_daily", "missing"), ("index_daily", "missing"),
                     ("stock_news", "missing"), ("daily_klines", "ok")])
    n = _pipeline(db)._alert_missing(db.get_connection(), MISS)
    assert n == 3, "返回值应为 missing 表数 (供调用方记账)"
    assert len(_alerts(db)) == 1, "逐表轰炸 = 把告警面变成噪音 (AUD-019 教训)"


def test_alert_detail_names_every_missing_table(db):
    """表名必须进 detail —— 只说「有表缺数」的告警等于没说。"""
    _seed(db, MISS, [("lhb_daily", "missing"), ("index_daily", "missing")])
    _pipeline(db)._alert_missing(db.get_connection(), MISS)
    detail = _alerts(db)[0][2]
    assert "lhb_daily" in detail and "index_daily" in detail
    assert MISS in detail, "detail 必须带审计日, 否则读的人不知道是哪天"


def test_alert_title_is_stable_across_counts(db):
    """title 跨 N 稳定 —— AlertManager 靠同名 open 抑制去重。

    title 里塞数量 → 每天一条新 open → 积压天天涨, 正是 AUD-019 的成因。
    """
    _seed(db, "2026-09-01", [("a", "missing")])
    _seed(db, "2026-09-02", [("a", "missing"), ("b", "missing")])
    p = _pipeline(db)
    p._alert_missing(db.get_connection(), "2026-09-01")
    t1 = _alerts(db)[0][1]
    conn = db.get_connection()
    conn.execute("DELETE FROM alerts")
    conn.commit()
    p._alert_missing(conn, "2026-09-02")
    t2 = _alerts(db)[0][1]
    assert t1 == t2, f"title 随数量变了: {t1!r} vs {t2!r}"


def test_clean_day_sends_no_alert(db):
    """反面: 全 ok 的日子不许发告警 (发了就是噪音源)。"""
    _seed(db, MISS, [("daily_klines", "ok"), ("lhb_daily", "ok")])
    assert _pipeline(db)._alert_missing(db.get_connection(), MISS) == 0
    assert _alerts(db) == []


# ------------------------------------------------------------ 对账守卫 (机器断言)

def test_guard_missing_without_alert_is_detectable(db):
    """**把「判了没广播」做成可断言的对账** —— 这是本件真正的防复发装置。

    判据: 当日 freshness 有 missing 行, 就必须有对应告警。
    不做成断言的话, 下次断线又是几个月无人知 (lhb 这次就是)。
    """
    from data_layer.pipelines.freshness_audit import reconcile_missing_alerts
    _seed(db, MISS, [("lhb_daily", "missing")])

    rep = reconcile_missing_alerts(db.get_connection(), MISS)
    assert rep["missing"] == 1 and rep["alerted"] is False, "此刻尚未广播"

    _pipeline(db)._alert_missing(db.get_connection(), MISS)
    rep2 = reconcile_missing_alerts(db.get_connection(), MISS)
    assert rep2["alerted"] is True, "广播后对账必须转绿"


# ------------------------------------------------------------ 接线面 (建了没通电)

def test_run_is_wired_to_alert():
    """**防「建了没通电」**: run() 必须真调 _alert_missing。

    锚在**代码面**不是注释面 (判例: inspect 断言须锚代码)。
    走真 run() 需要 DataReadiness 的整套表, 那是另一个夹具窄面坑;
    这里只锚"调用存在", 行为面由上面四条测试覆盖。
    """
    import inspect

    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
    src = inspect.getsource(FreshnessAuditPipeline.run)
    code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    assert any("_alert_missing(" in l for l in code_lines), \
        "run() 没接 _alert_missing = 建了没通电"


def test_alert_failure_does_not_break_audit_but_is_logged(db, monkeypatch, caplog):
    """fail-safe 但**不静默**: 告警炸了不阻断审计, 但必须留痕。"""
    import ops_layer.alert_manager as am
    _seed(db, MISS, [("lhb_daily", "missing")])

    def boom(self, alert):
        raise RuntimeError("probe: 告警通道故障")

    monkeypatch.setattr(am.AlertManager, "send", boom)
    with caplog.at_level("WARNING"):
        n = _pipeline(db)._alert_missing(db.get_connection(), MISS)
    assert n == 1, "告警失败不许吞掉 missing 计数"
    assert any("probe" in r.message or "告警" in r.message for r in caplog.records), \
        "告警失败静默 = 换个地方再犯同一个病"
