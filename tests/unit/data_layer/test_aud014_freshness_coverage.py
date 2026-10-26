"""AUD-014a 回归: index_daily 必须进冷采集新鲜度清单。

病根: index_daily 是 ddp (15:30) 的条件子管道, 每日应有行; 但它既不在
REQUIRED_TABLES 也不在 COLLECT_TABLES -> freshness_audit 完全不看它,
缺日零痕 (与 #116 lhb_daily "缺日 11 个交易日全盲" 同族)。
生产实测 2026-09-14: index_daily MAX(date)=2026-09-10, 落后 1 个交易日, 无人知。

注: adjust_factors 故意不在本卡 —— 它是 18:30 顶层腿, 而 freshness_audit
16:22 跑, 直接入清单会每日假 missing (需时点感知的期望值, 另出提案)。
"""
from common.database import Database
from data_layer.health.readiness import DataReadiness


def _db_with_index_daily(rows, kline_dates=()):
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE index_daily (code TEXT, date TEXT, close REAL)")
    # W2 卡①: index_daily 是 T+1 源, 判据=「上一交易日有行」,
    # 交易日历锚走 daily_klines (生产恒非空, 夹具须补齐否则 prev=None 恒不新鲜)。
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
    for d in kline_dates:
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES ('600519', ?, 1.0)", (d,))
    for code, date in rows:
        conn.execute("INSERT INTO index_daily (code, date, close) VALUES (?, ?, 1.0)",
                     (code, date))
    conn.commit()
    return db


def test_index_daily_is_audited_for_freshness():
    """当日有行 -> fresh=True 且 index_daily 出现在审计结果里。"""
    # 审计日 = 下一交易日 (T+1 源在审计时当日行未落是天然时序);
    # 上一交易日 (09-11) 有行 → fresh (这正是 #115 假阳被修掉的样子)。
    db = _db_with_index_daily([("000001", "2026-09-11"), ("399001", "2026-09-11")],
                              kline_dates=["2026-09-11", "2026-09-14"])
    result = DataReadiness(db).check_collect_freshness("2026-09-14")
    assert "index_daily" in result, "index_daily 未进新鲜度清单 -> 缺日零痕"
    assert result["index_daily"]["fresh"] is True
    assert result["index_daily"]["rows"] == 2


def test_index_daily_missing_day_is_visible():
    """当日无行 -> fresh=False (缺日必须留痕, 不得静默)。"""
    # 上一交易日 (09-11) 无行 = 源该发未发 → missing (真断供语义保留)。
    db = _db_with_index_daily([("000001", "2026-09-10")],
                              kline_dates=["2026-09-11", "2026-09-14"])
    result = DataReadiness(db).check_collect_freshness("2026-09-14")
    assert "index_daily" in result
    assert result["index_daily"]["fresh"] is False
    assert result["index_daily"]["rows"] == 0
