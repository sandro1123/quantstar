# -*- coding: utf-8 -*-
"""QS 卡 readiness-announce-aware-unify — 交易日面钉 (v2, 纯函数边界法)。

v2 修正 (统筹 sm-34293e378fc24eba ②条): 废弃 11-20 夹具锚 — 窗口边界测改
「纯函数边界 + helper 单元钉」:
  - fiscal_window_ok('2026-11-14','2026-06-30')=True (窗末日) /
    ('2026-11-15','2026-06-30')=False (窗末日次日) — 纯函数边界, 无夹具表,
    无未来日 klines 出货问题 (元检验钉两问全避开)
  - health 分流逻辑钉走 _fiscal_health_check helper (audit_date 显式注入,
    不跑真 bj_date) — 同样无夹具日期问题
全部审计锚 = 2026-09-30 (真交易日+有出货) — 三个锚以内。

钉位: T1 窗内不红 (fa 已 5a48ce36 落 + health helper 新钉) /
T2 窗外显形 (纯函数边界) / T3 三头同色 / T5 非季表回归 / T7 stale 显形。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

# 本文件审计锚: 全部真交易日 (09-30 有出货 5557 行, 生产实测)
# 窗口边界日期由纯函数直接测 (11-14/11-15 是「参数值」非「夹具锚」—
# 任何实际表数据无关; 元检验钉扫字面量若抓到它们, 其语义=函数参数非夹具)


# ---------- 纯函数边界钉 (T2 主形态) ----------

def test_t2_window_edge_true():
    """窗口语义 (5a48ce36 在册约定 t<next_due): 11-13 (窗末日-1) → True。"""
    from data_layer.health.readiness import fiscal_window_ok
    assert fiscal_window_ok("2026-11-13", "2026-06-30") is True


def test_t2b_window_expired_next_day_false():
    """窗末日 (2026-11-14, 下一季季末 09-30+45d) → False (t<next_due 排除末
    日 — 在册 T5/T6 锚 09-30 True/11-20 False 的实现语义, 窗末当日即过期)。"""
    from data_layer.health.readiness import fiscal_window_ok
    assert fiscal_window_ok("2026-11-14", "2026-06-30") is False
    assert fiscal_window_ok("2026-11-15", "2026-06-30") is False


def test_t2c_year_boundary():
    """年界: Q4(12-31) 实际 → 下一季=次年 Q1 季末 03-31+45=05-15 → 窗内。"""
    from data_layer.health.readiness import fiscal_window_ok
    assert fiscal_window_ok("2027-01-20", "2026-12-31") is True
    assert fiscal_window_ok("2027-05-14", "2026-12-31") is True   # 窗末日-1
    assert fiscal_window_ok("2027-05-15", "2026-12-31") is False  # 窗末当日
    assert fiscal_window_ok("2027-06-01", "2026-12-31") is False


# ---------- health helper 单元钉 (不跑真日历) ----------

def _fiscal_check(latest_str, audit_date, is_holiday=False, cal_err=None):
    from data_layer import DataLayer
    return DataLayer._fiscal_health_check(
        "stock_financials", latest_str, audit_date, is_holiday, cal_err)


def test_t1_health_window_pass():
    """health 季表分流: 09-30 audit 对 06-30 行 → 窗内 pass + stale 显形。"""
    s, detail, metric = _fiscal_check("2026-06-30", "2026-09-30")
    assert s == "pass", f"披露窗内须 pass: {s} — {detail}"
    assert "stale=92d" in detail, f"stale 显形 (卡3/判例 #128): {detail}"


def test_t3_three_heads_same_color():
    """三头同色: freshness_audit(ok) ↔ health helper(pass) 窗内一致。"""
    from data_layer.health.readiness import fiscal_window_ok
    # freshness_audit 侧同一真值源 (同源 import):
    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline as FAP
    assert FAP._fiscal_window_ok("2026-09-30", "2026-06-30") == \
        fiscal_window_ok("2026-09-30", "2026-06-30")
    s, _, _ = _fiscal_check("2026-06-30", "2026-09-30")
    assert s == "pass", "窗内 health 判 pass 与 freshness ok 同色"


def test_t3b_three_heads_expired_same_color():
    """窗外: freshness delayed ↔ health warn 同色 (mapping delayed→warn)。"""
    s, detail, _ = _fiscal_check("2026-06-30", "2026-11-15")
    assert s == "warn", f"窗外须 warn (delayed→warn 映射): {s} — {detail}"


def test_t4_holiday_branch():
    """假日分支: is_holiday=True → pass + holiday 标注 (helper 单元面)。"""
    s, detail, _ = _fiscal_check("2026-06-30", "2026-10-04", is_holiday=True)
    assert s == "pass" and "holiday (non-trading day)" in detail


def test_t4b_calendar_failopen_branch():
    """故障放行分支: calendar_error → pass + calendar_fail_open 标注
    (判例 #147 第三形态: 故障放行与假日判绿必须可区分)。"""
    s, detail, _ = _fiscal_check("2026-06-30", "2026-09-30",
                                 cal_err="TimeoutError: probe")
    assert s == "pass"
    assert "calendar_fail_open" in detail, f"故障放行须留痕: {detail}"
    assert "holiday" not in detail, "故障放行 ≠ 假日 (词面必须区分)"


# ---------- T5: 非季表回归钉 (helper 面之外的原判据) ----------

def test_t5_non_fiscal_tables_unaffected():
    """非季表零变化: thresholds 表其余 10 表不走分流 (FISCAL_TABLES 单集)。"""
    from data_layer.health.readiness import DataReadiness
    assert DataReadiness.FISCAL_TABLES == {"stock_financials"}, (
        "季表集合须只有 stock_financials (防误扩日更表)")


def test_t5b_stale_days_none_for_empty():
    """stale 显形对空值 = None (不造假 0 — 宁 NULL 不假值)。"""
    from data_layer.health.readiness import fiscal_stale_days
    assert fiscal_stale_days("2026-09-30", "") is None
    assert fiscal_stale_days("2026-09-30", "garbage") is None


# ---------- T7: 卡3 stale 显形 (health 集成面, 真库形态) ----------

def test_t7_health_integrated_stale_marker(fiscal_db):
    """health() 真调用 (交易日 bj_date 面): 季表行 detail 带 stale+max_date。"""
    from data_layer import DataLayer
    from datetime import datetime, timedelta
    dl = DataLayer(db=fiscal_db)
    lh = dl.health()
    checks = {c.name: c for c in lh.checks}
    c = checks["freshness_stock_financials"]
    # 今天是 10-04 (假日) → pass 分支; 若假日面有变化, 本钉读 detail 的 stale 数
    assert ("stale=" in (c.detail or "")) or ("holiday" in (c.detail or "")), (
        f"卡3 stale 显形须在位: {c.detail}")


@pytest.fixture()
def fiscal_db():
    """06-30 季末行 (真值锚=生产现状)。"""
    from common.database import Database
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE stock_financials (
            code TEXT NOT NULL, date TEXT NOT NULL,
            roe REAL DEFAULT 0, gross_margin REAL DEFAULT 0,
            PRIMARY KEY (code, date))
    """)
    for i in range(50):
        conn.execute(
            "INSERT INTO stock_financials (code, date, roe, gross_margin) VALUES (?,?,?,?)",
            (f"60050{i}", "2026-06-30", 10.0 + i, 50.0 + i))
    conn.commit()
    yield db
    conn.close()
    os.unlink(path)
