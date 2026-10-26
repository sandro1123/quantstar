# -*- coding: utf-8 -*-
"""REC 模块3 (2026-09-21): 推荐战绩沉淀 (对用户可读, 带基准)。

统筹卡断点③: retrospect_settlement 有 hit_5d/10d + AG-01 判定点, 但没拧成
对用户带基准的可读战绩 (有结算无呈现)。

生产现态 (2026-09-21 实查): recommendation_retrospect 1085 行 (settled 874),
verdict 分布 推荐=1 / 不推荐=262 / 观望=807 / 警惕=15 → **n<30 是现实,
样本量诚实标注是主语义不是边角**。对失败诚实: 推荐错了 (hit=0) 也展示。

基准 (统筹卡): 同期沪深300 (index_daily 000300.SH, 5995 行) + 随机50%基线
(无方向主张瞎推的先验命中概率)。超额收益 = 平均 fwd_return − 同期指数收益。
"""
import sqlite3

import pytest

from common.database import Database


# 判例#8
def test_module_exists():
    try:
        import report_layer.track_record  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"report_layer.track_record 未建: {e}") from e


def _db():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE recommendation_retrospect (
            prediction_id INTEGER PRIMARY KEY, request_id TEXT, agent_name TEXT,
            code TEXT, rec_date TEXT, verdict TEXT, confidence REAL,
            fwd_return_5d REAL, fwd_return_10d REAL,
            hit_5d INTEGER, hit_10d INTEGER, settled_at TEXT);
        CREATE TABLE index_daily (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
    """)
    conn.commit()
    return db, conn


def _seed(conn, rows):
    for r in rows:
        conn.execute(
            "INSERT INTO recommendation_retrospect (request_id, agent_name, code, rec_date, "
            "verdict, confidence, fwd_return_5d, hit_5d, settled_at) "
            "VALUES ('r','a',?,?,?,?,?,?,'2026-09-01')",
            (r["code"], r["date"], r["verdict"], r.get("conf", 0.8),
             r.get("fwd5"), r.get("hit5", 1 if (r["verdict"] == "推荐") == (r.get("fwd5", 0) > 0) else 0)))
    conn.commit()


# ---- 核心判据 ----

def test_summary_small_sample_honest_label():
    """n<30 → 样本量诚实标注 (verdict=推荐 生产现实就是 n=1)。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    _seed(conn, [
        {"code": "600001", "date": "2026-09-10", "verdict": "推荐", "fwd5": 0.03},
    ])
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["sample_size"] == 1
    assert summary["sample_adequate"] is False, "n=1 必须标样本不足"
    assert "样本不足" in summary["sample_note"]


def test_summary_adequate_sample():
    """n>=30 → sample_adequate=True。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    _seed(conn, [
        {"code": f"6000{i:02d}", "date": "2026-09-10", "verdict": "推荐",
         "fwd5": 0.02 if i % 2 else -0.01}
        for i in range(30)
    ])
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["sample_size"] == 30
    assert summary["sample_adequate"] is True


def test_win_rate_and_hit_semantics():
    """胜率 = hit 比例; 对失败诚实 — hit=0 照在结果里。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    rows = [
        {"code": f"6000{i:02d}", "date": "2026-09-10", "verdict": "推荐",
         "fwd5": 0.02, "hit5": 1}
        for i in range(18)
    ] + [
        {"code": f"6001{i:02d}", "date": "2026-09-10", "verdict": "推荐",
         "fwd5": -0.01, "hit5": 0}
        for i in range(12)
    ]
    _seed(conn, rows)
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["sample_size"] == 30
    assert abs(summary["win_rate"] - 0.6) < 1e-6, "胜率 18/30=0.6"
    assert summary["miss_count"] == 12, "失败数必须如实呈现 (对失败诚实)"


def test_benchmark_hs300_excess_return():
    """超额收益 = 平均 fwd_return − 同期沪深300 (rec_date→T+5 指数收益)。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    # 指数: 09-10 收 4000, 5 个交易日后 09-17 收 4040 → 基准收益 1%
    for d, close in (("2026-09-09", 3990), ("2026-09-10", 4000),
                     ("2026-09-11", 4010), ("2026-09-14", 4020),
                     ("2026-09-15", 4030), ("2026-09-16", 4035),
                     ("2026-09-17", 4040)):
        conn.execute("INSERT INTO index_daily (code, date, close) VALUES ('000300.SH', ?, ?)",
                     (d, close))
    # 30 条推荐, 全部 09-10, fwd 均值 2% → 超额 = 2% - 1% = 1%
    _seed(conn, [
        {"code": f"6000{i:02d}", "date": "2026-09-10", "verdict": "推荐", "fwd5": 0.02}
        for i in range(30)
    ])
    conn.commit()
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["benchmark_return"] is not None, "基准收益未算"
    assert abs(summary["benchmark_return"] - 0.01) < 1e-6, \
        f"基准={summary['benchmark_return']} 期望 1% (4000→4040)"
    assert abs(summary["excess_return"] - 0.01) < 1e-6, \
        f"超额={summary['excess_return']} 期望 2%-1%=1%"


def test_random_baseline_reported():
    """随机50%基线必须出现 (统筹卡: 同期沪深300 + 随机50% 双基准)。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    _seed(conn, [
        {"code": f"6000{i:02d}", "date": "2026-09-10", "verdict": "推荐", "fwd5": 0.02}
        for i in range(30)
    ])
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["random_baseline"] == 0.5, "随机基线=0.5 (无方向主张瞎推的先验)"
    assert summary["win_rate"] > summary["random_baseline"], "有意义的战绩须跑赢瞎推"


def test_empty_verdict_returns_honest_zero():
    """无该 verdict 结算样本 → 零样本诚实形态 (不造假, census_guard 判例)。"""
    from report_layer.track_record import build_track_record
    db, conn = _db()
    summary = build_track_record(conn, verdict="推荐", asof="2026-09-20")
    assert summary["sample_size"] == 0
    assert summary["win_rate"] is None, "零样本胜率须 None (不得输出 0% 造数据)"
    assert ("无样本" in summary["sample_note"]) or ("无已结算" in summary["sample_note"])
