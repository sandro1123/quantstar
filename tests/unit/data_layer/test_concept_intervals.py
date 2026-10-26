"""区间存储 concept_membership + plate_daily_rate — 验收测试 (统筹 sm-337ab3642f7f486f)

验收点:
1. 适配器 _get_concept_intervals: 保留 time_in/time_out 原始区间 (不做当日 PIT
   过滤), rate 拿不到写 NULL 行不写 0.0 也不跳过 (留"当日在榜"痕迹),
   回补打标 _backfill, time_in 缺失行跳过并计数, 空板块计数。
2. 管道: stock_info 宇宙过滤 + 计数, 宇宙为空拒绝写库, 出板事件幂等覆盖
   (time_out NULL→有值), 0 条 fail loud, 写锁持有时间落 metadata。
3. 读侧 get_concept_data 严格同日 PIT: 区间表在场走 membership 时点判定 +
   plate_daily_rate 当日 rate; 区间表不在场走 concept_daily 同日兜底
   (date == 查询日, 不再跨日 LIMIT 10 — concept_count 假活根因)。
4. DDL 约束⑤: 建表语句不含任何 DEFAULT 子句 (宁可 NULL 不可假值)。
5. GO-7 硬闸: 注册进 PipelineRegistry, 不接进 orchestrator sub_pipelines。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from data_layer._loader import DataLoader
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.concept_intervals import (
    CONCEPT_INTERVAL_INDEXES,
    CONCEPT_MEMBERSHIP_DDL,
    PLATE_DAILY_RATE_DDL,
    ConceptIntervalsPipeline,
)
from data_layer.sources.zzshare_source import (
    BACKFILL_SOURCE_SUFFIX,
    PLATE_TYPE_CONCEPT,
    PLATE_TYPE_THEME,
    ZZShareSourceAdapter,
)

# ---------------------------------------------------------------------------
# 适配器: _get_concept_intervals
# ---------------------------------------------------------------------------

def _make_adapter(plates_by_type, stocks_by_key, rank_by_type):
    adapter = ZZShareSourceAdapter()
    client = MagicMock()
    client.plates_list.side_effect = lambda plate_type: plates_by_type.get(plate_type, [])
    client.plates_stocks.side_effect = (
        lambda plate_type, plate_code, date: stocks_by_key.get((plate_type, plate_code), []))
    client.plates_rank.side_effect = lambda plate_type, date1, limit: rank_by_type.get(plate_type, [])
    adapter._client = client
    return adapter


def test_intervals_keep_time_out_not_pit_filtered():
    """区间保留: 已出板成员 (time_out<=date) 也返回, 不做当日 PIT 过滤。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""},
            {"stock_code": "000003", "time_in": "2020-01-01", "time_out": "2025-01-01"},  # 已出板也保留
            {"stock_code": "000002", "time_in": "2026-09-01", "time_out": ""},  # 未来进板 → 过滤
        ]},
        rank_by_type={},
    )
    out = adapter._get_concept_intervals({"date": "2026-08-21", "sleep_s": 0})
    mem = {m["code"]: m for m in out["memberships"]}
    assert set(mem) == {"000001", "000003"}
    assert mem["000001"]["time_out"] is None  # 空 → NULL (仍在板)
    assert mem["000003"]["time_out"] == "2025-01-01"
    assert mem["000001"]["source"] == "zzshare_concept_15"


def test_rate_missing_writes_null_row_not_zero_not_skipped():
    """约束②: rate 拿不到写 NULL 行 (留当日在榜痕迹), 禁止 0.0, 不跳过。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C1", "rate": None},      # 在榜但 rate 缺失
            {"plate_code": "C2", "rate": 1.5},       # 正常
        ]},
    )
    out = adapter._get_concept_intervals({"date": "2026-08-21", "sleep_s": 0})
    rates = {r["plate_code"]: r for r in out["rates"]}
    assert "C1" in rates and rates["C1"]["rate"] is None  # NULL 行, 不是跳过
    assert rates["C2"]["rate"] == pytest.approx(1.5)
    assert out["stats"]["rank_missing"] == 1


def test_interval_backfill_flag_suffix():
    """回补打标: memberships 与 rates 的 source 都加 _backfill (D-104 幸存者偏差)。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_THEME: [{"plate_code": "T1", "plate_name": "AI智能体"}]},
        stocks_by_key={(PLATE_TYPE_THEME, "T1"): [
            {"stock_code": "000002", "time_in": "2024-06-01", "time_out": ""}]},
        rank_by_type={PLATE_TYPE_THEME: [{"plate_code": "T1", "rate": -1.2}]},
    )
    out = adapter._get_concept_intervals({"date": "2026-08-11", "sleep_s": 0, "backfill": True})
    assert out["memberships"][0]["source"] == "zzshare_theme_17" + BACKFILL_SOURCE_SUFFIX
    assert out["rates"][0]["source"] == "zzshare_theme_17" + BACKFILL_SOURCE_SUFFIX


def test_time_in_missing_rows_skipped_and_counted():
    """time_in 缺失无法构成区间 (PK 含 time_in) → 跳过并计数。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""},
            {"stock_code": "000004", "time_in": "", "time_out": ""},
        ]},
        rank_by_type={},
    )
    out = adapter._get_concept_intervals({"date": "2026-08-21", "sleep_s": 0})
    assert [m["code"] for m in out["memberships"]] == ["000001"]
    assert out["stats"].get("time_in_missing") == 1


def test_interval_empty_plate_counted():
    """约束④: 空板块跳过并计数。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C1", "plate_name": "芯片"},
            {"plate_code": "C2", "plate_name": "空板块"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={},
    )
    out = adapter._get_concept_intervals({"date": "2026-08-21", "sleep_s": 0})
    assert out["stats"]["plates_empty"] == 1
    assert out["stats"]["plates_ok"] == 1


# ---------------------------------------------------------------------------
# DDL 约束⑤: 不写任何 DEFAULT 子句
# ---------------------------------------------------------------------------

def test_ddl_has_no_default_clauses():
    """约束⑤ (DEFAULT 'qfq' 教训): 建表 DDL 不得含 DEFAULT — 缺省即 NULL。"""
    for ddl in (CONCEPT_MEMBERSHIP_DDL, PLATE_DAILY_RATE_DDL):
        assert "DEFAULT" not in ddl.upper(), f"DDL 含 DEFAULT 子句: {ddl}"


# ---------------------------------------------------------------------------
# 管道: 宇宙过滤 / 幂等覆盖 / fail loud
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE stock_info (code TEXT, industry TEXT)")
        self._conn.commit()

    def get_connection(self):
        return self._conn


def _pipeline_with_data(db, memberships, rates=None, stats=None):
    pipe = ConceptIntervalsPipeline(db=db)
    pipe._fetch_intervals = lambda params: (
        {"memberships": memberships, "rates": rates or [],
         "stats": stats or {}}, "zzshare")
    return pipe


def _seed_universe(db, codes):
    conn = db.get_connection()
    conn.executemany("INSERT INTO stock_info (code) VALUES (?)", [(c,) for c in codes])
    conn.commit()


def test_pipeline_filters_to_universe_and_counts():
    """约束③: 宇宙外成员区间过滤 + 计数。"""
    db = _FakeDB()
    _seed_universe(db, ["000001"])
    memberships = [
        {"code": "000001", "concept_name": "芯片", "plate_code": "C1",
         "plate_type": 15, "source": "zzshare_concept_15",
         "time_in": "2020-01-01", "time_out": None},
        {"code": "999999", "concept_name": "芯片", "plate_code": "C1",
         "plate_type": 15, "source": "zzshare_concept_15",
         "time_in": "2020-01-01", "time_out": None},
    ]
    result = _pipeline_with_data(db, memberships).execute(PipelineInput(date="2026-08-21"))
    assert result.status == "success"
    assert result.metadata["filtered_out"] == 1
    rows = db.get_connection().execute("SELECT code FROM concept_membership").fetchall()
    assert rows == [("000001",)]


def test_pipeline_empty_universe_refuses_write():
    db = _FakeDB()
    memberships = [{"code": "000001", "concept_name": "芯片", "plate_code": "C1",
                    "plate_type": 15, "source": "zzshare_concept_15",
                    "time_in": "2020-01-01", "time_out": None}]
    result = _pipeline_with_data(db, memberships).execute(PipelineInput(date="2026-08-21"))
    assert result.status == "failed"
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM concept_membership").fetchone()[0] == 0


def test_pipeline_writes_null_rate_and_time_out():
    """约束②: rate NULL 照写; time_out None → NULL (仍在板)。"""
    db = _FakeDB()
    _seed_universe(db, ["000001"])
    memberships = [{"code": "000001", "concept_name": "芯片", "plate_code": "C1",
                    "plate_type": 15, "source": "zzshare_concept_15",
                    "time_in": "2020-01-01", "time_out": None}]
    rates = [{"plate_code": "C1", "plate_type": 15, "date": "2026-08-21",
              "rate": None, "source": "zzshare_concept_15"}]
    result = _pipeline_with_data(db, memberships, rates).execute(
        PipelineInput(date="2026-08-21"))
    assert result.status == "success"
    conn = db.get_connection()
    assert conn.execute("SELECT time_out FROM concept_membership").fetchone()[0] is None
    assert conn.execute("SELECT rate FROM plate_daily_rate").fetchone()[0] is None


def test_pipeline_exit_event_idempotent_overwrite():
    """出板事件: 重跑时 time_out 由 NULL 变有值, INSERT OR REPLACE 幂等覆盖, 行数不增。"""
    db = _FakeDB()
    _seed_universe(db, ["000001"])
    base = {"code": "000001", "concept_name": "芯片", "plate_code": "C1",
            "plate_type": 15, "source": "zzshare_concept_15",
            "time_in": "2020-01-01"}
    _pipeline_with_data(db, [dict(base, time_out=None)]).execute(
        PipelineInput(date="2026-08-21"))
    _pipeline_with_data(db, [dict(base, time_out="2026-08-21")]).execute(
        PipelineInput(date="2026-08-22"))
    conn = db.get_connection()
    rows = conn.execute("SELECT time_out FROM concept_membership").fetchall()
    assert rows == [("2026-08-21",)]  # 覆盖, 不是新增第二行


def test_pipeline_zero_memberships_fails_loud():
    db = _FakeDB()
    result = _pipeline_with_data(db, []).execute(PipelineInput(date="2026-08-21"))
    assert result.status == "failed"
    assert result.errors


def test_pipeline_metadata_reports_write_lock_hold():
    db = _FakeDB()
    _seed_universe(db, ["000001"])
    memberships = [{"code": "000001", "concept_name": "芯片", "plate_code": "C1",
                    "plate_type": 15, "source": "zzshare_concept_15",
                    "time_in": "2020-01-01", "time_out": None}]
    result = _pipeline_with_data(db, memberships).execute(PipelineInput(date="2026-08-21"))
    assert result.metadata["write_lock_hold_s"] >= 0
    assert result.metadata["memberships_written"] == 1


def test_pipeline_dry_run_writes_nothing():
    """DL-32 基类拦截: dry_run 目标表行数不变。"""
    db = _FakeDB()
    _seed_universe(db, ["000001"])
    memberships = [{"code": "000001", "concept_name": "芯片", "plate_code": "C1",
                    "plate_type": 15, "source": "zzshare_concept_15",
                    "time_in": "2020-01-01", "time_out": None}]
    result = _pipeline_with_data(db, memberships).execute(
        PipelineInput(date="2026-08-21", dry_run=True))
    assert result.metadata.get("dry_run") is True
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM concept_membership").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 读侧: get_concept_data 严格同日 PIT
# ---------------------------------------------------------------------------

class _LoaderDB:
    """带 concept_daily + 可选区间表的 fake DB。"""

    def __init__(self, with_interval_tables: bool):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE concept_daily (code TEXT, date TEXT, concept_name TEXT, "
            "change_pct REAL, source TEXT DEFAULT '')")
        if with_interval_tables:
            self._conn.execute(CONCEPT_MEMBERSHIP_DDL)
            self._conn.execute(PLATE_DAILY_RATE_DDL)
            for idx_ddl in CONCEPT_INTERVAL_INDEXES:
                self._conn.execute(idx_ddl)
        self._conn.commit()

    def get_connection(self):
        return self._conn


def test_read_side_interval_strict_same_day_pit():
    """区间表在场: time_in<=date 且 (time_out NULL 或 >date) 才算当日在板。"""
    db = _LoaderDB(with_interval_tables=True)
    conn = db.get_connection()
    conn.executemany(
        "INSERT INTO concept_membership VALUES (?,?,?,?,?,?,?)", [
            ("000001", "芯片", "C1", 15, "zzshare_concept_15", "2020-01-01", None),
            ("000001", "军工", "C2", 15, "zzshare_concept_15", "2020-01-01", "2025-01-01"),  # 已出板
            ("000001", "AI智能体", "T1", 17, "zzshare_theme_17", "2026-09-01", None),  # 未来进板
            ("000001", "机器人", "C3", 15, "zzshare_concept_15", "2024-01-01", "2026-08-22"),  # 查询日后出板 → 在板
        ])
    conn.execute(
        "INSERT INTO plate_daily_rate VALUES ('C1', 15, '2026-08-21', 0.69, 'zzshare_concept_15')")
    conn.commit()
    result = DataLoader(db).get_concept_data("000001", "2026-08-21")
    names = {r.concept_name for r in result}
    assert names == {"芯片", "机器人"}
    heat = {r.concept_name: r.change_pct for r in result}
    assert heat["芯片"] == pytest.approx(0.69)
    assert heat["机器人"] == 0.0  # rate 无 → 模型默认 0.0 (与旧行为一致)


def test_read_side_interval_no_cross_day_leak():
    """旧病根钉住: 不再跨日取最近 N 行 (concept_count 假活根因)。"""
    db = _LoaderDB(with_interval_tables=False)
    conn = db.get_connection()
    conn.executemany("INSERT INTO concept_daily VALUES (?,?,?,?,?)", [
        ("000001", "2026-08-21", "芯片", 0.69, "zzshare_concept_15"),
        ("000001", "2026-08-20", "白酒", 0.3, "zzshare_concept_15"),   # 跨日行不得出现
        ("000001", "2026-08-19", "食品饮料", 0.1, "industry_fallback"),
    ])
    conn.commit()
    result = DataLoader(db).get_concept_data("000001", "2026-08-21")
    assert {r.concept_name for r in result} == {"芯片"}
    assert all(r.date == "2026-08-21" for r in result)


def test_read_side_fallback_when_interval_tables_absent():
    """区间表未建 (生产未 GO) → concept_daily 同日兜底, 不报错。"""
    db = _LoaderDB(with_interval_tables=False)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO concept_daily VALUES ('600519', '2026-08-21', '白酒', 0.3, 'zzshare_concept_15')")
    conn.commit()
    result = DataLoader(db).get_concept_data("600519", "2026-08-21")
    assert len(result) == 1
    assert result[0].concept_name == "白酒"


def test_read_side_no_limit():
    """统筹批准: 严格同日 PIT 无 LIMIT — limit 参数已删除 (§3 第六种死处置)。"""
    db = _LoaderDB(with_interval_tables=False)
    conn = db.get_connection()
    conn.executemany("INSERT INTO concept_daily VALUES (?,?,?,?,?)", [
        ("000001", "2026-08-21", f"概念{i}", 0.1, "zzshare_concept_15") for i in range(15)
    ])
    conn.commit()
    result = DataLoader(db).get_concept_data("000001", "2026-08-21")
    assert len(result) == 15  # 不被 limit=10 截断


# ---------------------------------------------------------------------------
# GO-7 硬闸 + 注册
# ---------------------------------------------------------------------------

def test_go7_gate_not_in_orchestrator_sub_pipelines():
    """🔴 GO-7: concept_intervals 不得出现在 orchestrator sub_pipelines。"""
    orch = (_root / "src" / "ops_layer" / "orchestrator.py").read_text(encoding="utf-8")
    import re
    # DL-51 重构后 sub_pipelines 改为模块级常量 DAILY_SUB_PIPELINES
    m = re.search(r"DAILY_SUB_PIPELINES\s*=\s*\[(.*?)\]", orch, re.S)
    assert m, "orchestrator DAILY_SUB_PIPELINES 常量未找到"
    assert "concept_intervals" not in m.group(1)


def test_registered_in_data_layer():
    init = (_root / "src" / "data_layer" / "__init__.py").read_text(encoding="utf-8")
    assert "ConceptIntervalsPipeline" in init
