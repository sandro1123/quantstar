"""PIT Phase 1: backfill_announce_date.match_announce_dates 匹配逻辑

纯函数测试, 无网络无写库。核心: 精确键优先, 未命中回退 date-1day 键
(fuyao period_end 恒为季末日前一天, 库内旧尾行用标准季末日, off-by-one)。
"""

from scripts.backfill_announce_date import match_announce_dates


def test_exact_key_hit():
    """库内 date 与 fuyao period_end 同口径 (近期批次) → 精确命中。"""
    need = {("000001", "2026-03-30")}
    amap = {("000001", "2026-03-30"): "2026-04-24"}
    updates, unmatched = match_announce_dates(need, amap)
    assert updates == [("2026-04-24", "000001", "2026-03-30")]
    assert unmatched == []


def test_off_by_one_fallback():
    """库内标准季末日 09-30, fuyao 键 09-29 → 回退命中。"""
    need = {("002731", "2025-09-30")}
    amap = {("002731", "2025-09-29"): "2025-10-27"}
    updates, unmatched = match_announce_dates(need, amap)
    assert updates == [("2025-10-27", "002731", "2025-09-30")]
    assert unmatched == []


def test_exact_key_preferred_over_fallback():
    """两键都在 → 用精确键的公告日, 不用回退键。"""
    need = {("000001", "2026-03-30")}
    amap = {("000001", "2026-03-30"): "2026-04-24", ("000001", "2026-03-29"): "1999-01-01"}
    updates, unmatched = match_announce_dates(need, amap)
    assert updates == [("2026-04-24", "000001", "2026-03-30")]


def test_both_miss_goes_unmatched():
    """精确键与回退键都无 → 进残余清单 (源端确无, 诚实落档)。"""
    need = {("600001", "2009-09-30")}
    amap = {}
    updates, unmatched = match_announce_dates(need, amap)
    assert updates == []
    assert unmatched == [("600001", "2009-09-30")]


def test_empty_need():
    updates, unmatched = match_announce_dates(set(), {})
    assert updates == [] and unmatched == []


def test_mixed_batch_sorted_and_partitioned():
    """混合批次: 命中/回退/未中各一, 输出按 (code,date) 排序且正确分区。"""
    need = {("300001", "2026-06-29"), ("002731", "2025-09-30"), ("000542", "2003-09-30")}
    amap = {
        ("300001", "2026-06-29"): "2026-08-20",
        ("002731", "2025-09-29"): "2025-10-27",
    }
    updates, unmatched = match_announce_dates(need, amap)
    assert updates == [("2025-10-27", "002731", "2025-09-30"), ("2026-08-20", "300001", "2026-06-29")]
    assert unmatched == [("000542", "2003-09-30")]
