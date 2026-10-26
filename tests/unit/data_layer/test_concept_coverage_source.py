"""卡#44 守卫修严: get_concept_coverage 只认 zzshare 真源 (R4 施工 ①, 统筹 sm-39c8b41402a748ee).

背景 (A 调研稿 分层讨论/concept每日通路断电-A-20260901.md R2-①):
  现 get_concept_coverage 只查「日期有行」不查 source — 08-28/31 两日
  5,892 行 industry_fallback 让 coverage=100% 假绿, walk_forward 守卫
  假通过 (最重级隐患)。

修法 (统筹裁定「先修守卫后通电」):
  covered 分子加 source 口径 — 只认 zzshare 真源 (zzshare_concept_15 /
  zzshare_theme_17, 含 _backfill 后缀); industry_fallback 行不计入。
  行业兜底行不再冒充覆盖证据 (行业冒充概念正是断电案病根)。
"""
import sqlite3

import pytest

from data_layer import DataLayer


@pytest.fixture()
def dl(tmp_path):
    """真 DataLayer + 临时库: trading 日历由 daily_klines 有行日构成。

    用真 Database (check_same_thread=False 同 repo 生产口径) 而非裸
    sqlite3.connect — 裸连接跨线程被 catch_quantstar_error 吞成 None
    (get_concept_coverage 有装饰器), 假 RED。
    """
    from common.database import Database
    path = str(tmp_path / "t.db")
    db = Database(path)
    conn = db.get_connection()
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
    for d in ("2026-08-27", "2026-08-28", "2026-08-31"):
        conn.execute("INSERT INTO daily_klines VALUES ('000001', ?, 10.0)", (d,))
    conn.execute("""CREATE TABLE concept_daily (
        code TEXT, date TEXT, concept_name TEXT, change_pct REAL,
        source TEXT DEFAULT '', PRIMARY KEY (code, date, concept_name))""")
    conn.commit()
    layer = DataLayer.__new__(DataLayer)  # 轻量: 不走 __init__ (不落盘建真库)
    layer.db = db
    return layer, db


def test_fallback_rows_do_not_count_as_covered(dl):
    """RED: 08-28/31 只有 industry_fallback 行 → 该两日不得计入 covered。"""
    layer, db = dl
    # 08-27 真源行; 08-28/31 纯 fallback 行 (生产现状复原)
    db.execute("INSERT INTO concept_daily VALUES ('000001','2026-08-27','AI','1.0','zzshare_concept_15')")
    for d in ("2026-08-28", "2026-08-31"):
        db.execute("INSERT INTO concept_daily VALUES ('000001',?,'银行','0.0','industry_fallback')", (d,))
    db.commit()
    coverage, missing = layer.get_concept_coverage("2026-08-27", "2026-08-31")
    assert coverage < 1.0, "industry_fallback 行不得计入 covered — 守卫假通过病根"
    assert set(missing) == {"2026-08-28", "2026-08-31"}


def test_backfill_true_source_counts_as_covered(dl):
    """绿灯: zzshare_concept_15_backfill / zzshare_theme_17_backfill 是真源,
    R3 回补后的两日必须计满 (回补数据合法覆盖)。"""
    layer, db = dl
    db.execute("INSERT INTO concept_daily VALUES ('000001','2026-08-27','AI','1.0','zzshare_concept_15')")
    db.execute("INSERT INTO concept_daily VALUES ('000001','2026-08-28','AI','1.0','zzshare_concept_15_backfill')")
    db.execute("INSERT INTO concept_daily VALUES ('000001','2026-08-31','机器人','1.0','zzshare_theme_17_backfill')")
    db.commit()
    coverage, missing = layer.get_concept_coverage("2026-08-27", "2026-08-31")
    assert coverage == 1.0 and not missing, (
        f"zzshare 真源 (含 _backfill 后缀) 须全额计入 covered, got {coverage} {missing}")


def test_mixed_day_true_source_wins(dl):
    """混合日 (同日真源+fallback 并存, R4 时序颠倒窗口): 真源在场即算覆盖 —
    守卫口径看「该日是否有真源行」, 不要求该日零 fallback 行
    (时序清理由 17:05 校验哨兵负责, 守卫只管概念数据可用性)。"""
    layer, db = dl
    for d in ("2026-08-27", "2026-08-28", "2026-08-31"):
        db.execute("INSERT INTO concept_daily VALUES ('000001',?,'AI','1.0','zzshare_concept_15')", (d,))
        db.execute("INSERT INTO concept_daily VALUES ('000001',?,'银行','0.0','industry_fallback')", (d,))
    db.commit()
    coverage, missing = layer.get_concept_coverage("2026-08-27", "2026-08-31")
    assert coverage == 1.0 and not missing


def test_empty_concept_no_klines_guard(dl):
    """无交易日历 (daily_klines 空窗) → 现状语义保持: 返回 1.0 不误报。"""
    layer, db = dl
    db.execute("DELETE FROM daily_klines")
    db.commit()
    coverage, missing = layer.get_concept_coverage("2026-09-01", "2026-09-30")
    assert coverage == 1.0 and missing == []
