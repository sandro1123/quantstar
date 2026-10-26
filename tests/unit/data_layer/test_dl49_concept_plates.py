"""DL-49: concept_plates 真概念恢复 — 验收测试

验收点:
1. 适配器: plate_type 15/17 分开留痕 (source 三态), change_pct 来自 plates_rank
   映射且缺失写 None 不写 0.0 (F-45 教训), 时点在板判定 (time_in/time_out),
   空板块跳过计数, 回补打标 _backfill (幸存者偏差, 统筹裁定)。
2. 管道: stock_info 宇宙过滤 + 计数 (341 事件探针), 宇宙为空拒绝写库,
   真概念写入当日清掉 industry_fallback 行, 写锁持有时间落 metadata,
   拉取 0 行 fail loud 不静默。
3. GO-7 硬闸: 管道注册进 PipelineRegistry, 但**不接进 orchestrator sub_pipelines**。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.concept_plates import ConceptPlatesPipeline
from data_layer.sources.zzshare_source import (
    BACKFILL_SOURCE_SUFFIX,
    PLATE_TYPE_CONCEPT,
    PLATE_TYPE_THEME,
    SOURCE_LABEL_BY_PLATE_TYPE,
    ZZShareSourceAdapter,
)

# ---------------------------------------------------------------------------
# 适配器: _get_concept_plates
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


def test_source_labels_split_by_plate_type():
    """前置②: 15/17 分开留痕, 行业(14)不采。"""
    assert SOURCE_LABEL_BY_PLATE_TYPE[PLATE_TYPE_CONCEPT] == "zzshare_concept_15"
    assert SOURCE_LABEL_BY_PLATE_TYPE[PLATE_TYPE_THEME] == "zzshare_theme_17"
    assert 14 not in SOURCE_LABEL_BY_PLATE_TYPE


def test_records_carry_source_and_change_pct_from_rank():
    adapter = _make_adapter(
        plates_by_type={
            PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}],
            PLATE_TYPE_THEME: [{"plate_code": "T1", "plate_name": "AI智能体"}],
        },
        stocks_by_key={
            (PLATE_TYPE_CONCEPT, "C1"): [
                {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}],
            (PLATE_TYPE_THEME, "T1"): [
                {"stock_code": "000002", "time_in": "2024-06-01", "time_out": ""}],
        },
        rank_by_type={
            PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "rate": 0.69}],
            PLATE_TYPE_THEME: [{"plate_code": "T1", "rate": -1.2}],
        },
    )
    out = adapter._get_concept_plates({"date": "2026-08-21", "sleep_s": 0})
    recs = {(r["code"], r["concept_name"]): r for r in out["records"]}
    assert recs[("000001", "芯片")]["source"] == "zzshare_concept_15"
    assert recs[("000001", "芯片")]["change_pct"] == pytest.approx(0.69)
    assert recs[("000002", "AI智能体")]["source"] == "zzshare_theme_17"
    assert recs[("000002", "AI智能体")]["change_pct"] == pytest.approx(-1.2)
    assert out["stats"]["plates_ok"] == 2
    assert out["stats"]["errors"] == []


def test_change_pct_none_when_rank_missing_not_zero():
    """前置③ (F-45 教训): rank 映射没有的板块 change_pct = None, 禁止 0.0。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={},  # plates_rank 空 → 无映射
    )
    out = adapter._get_concept_plates({"date": "2026-08-21", "sleep_s": 0})
    assert len(out["records"]) == 1
    assert out["records"][0]["change_pct"] is None
    assert out["stats"]["rank_missing"] == 1


def test_point_in_time_membership_filter():
    """时点判定: time_in<=date 且 (time_out 空 或 time_out>date) 才算当日在板。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [{"plate_code": "C1", "plate_name": "芯片"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""},          # 在板
            {"stock_code": "000002", "time_in": "2026-09-01", "time_out": ""},          # 未来进板
            {"stock_code": "000003", "time_in": "2020-01-01", "time_out": "2025-01-01"},  # 已出板
            {"stock_code": "000004", "time_in": "2020-01-01", "time_out": "2026-08-22"},  # 查询日后出板 → 在板
        ]},
        rank_by_type={},
    )
    out = adapter._get_concept_plates({"date": "2026-08-21", "sleep_s": 0})
    codes = {r["code"] for r in out["records"]}
    assert codes == {"000001", "000004"}


def test_empty_plate_skipped_and_counted():
    """统筹附加③: 空板块跳过并计数 (暴增即源侧异常信号)。"""
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C1", "plate_name": "芯片"},
            {"plate_code": "C2", "plate_name": "空板块"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={},
    )
    out = adapter._get_concept_plates({"date": "2026-08-21", "sleep_s": 0})
    assert out["stats"]["plates_empty"] == 1
    assert out["stats"]["plates_ok"] == 1


def test_backfill_flag_adds_source_suffix():
    """统筹裁定: 回补数据打标 _backfill (纯溯源标记)。

    无偏回补 (统筹 sm-0e53a77503a841fe GO): backfill=True 时宇宙来自 plates_rank
    (当日活跃板块, PIT 含已删板块), 不再用 plates_list 当前目录 (前视宇宙)。
    """
    adapter = _make_adapter(
        plates_by_type={},  # backfill 不读 plates_list
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C1", "plate_name": "芯片", "rate": 0.69}]},
    )
    out = adapter._get_concept_plates({"date": "2026-08-11", "sleep_s": 0, "backfill": True})
    assert out["records"][0]["source"] == "zzshare_concept_15" + BACKFILL_SOURCE_SUFFIX


def test_backfill_universe_from_rank_recovers_deleted_plates():
    """无偏实证: backfill 宇宙=plates_rank(date), 含已删板块 (不在 plates_list)。

    构造: plates_list 只有 C_ALIVE; plates_rank(历史日) 含 C_ALIVE + C_DELETED。
    旧实现 (plates_list 宇宙) 会漏掉 C_DELETED → 幸存者偏差;
    新实现 (plates_rank 宇宙) 两者都采 → 无偏。
    """
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C_ALIVE", "plate_name": "现存板块"}]},
        stocks_by_key={
            (PLATE_TYPE_CONCEPT, "C_ALIVE"): [
                {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}],
            (PLATE_TYPE_CONCEPT, "C_DELETED"): [
                {"stock_code": "000002", "time_in": "2020-01-01", "time_out": ""}],
        },
        rank_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C_ALIVE", "plate_name": "现存板块", "rate": 1.0},
            {"plate_code": "C_DELETED", "plate_name": "已删板块", "rate": 2.0}],
        },
    )
    out = adapter._get_concept_plates({"date": "2021-01-04", "sleep_s": 0, "backfill": True})
    names = {r["concept_name"] for r in out["records"]}
    # 已删板块必须入库 → 无偏
    assert "已删板块" in names, f"deleted plate missing, got {names}"
    assert "现存板块" in names
    assert len(out["records"]) == 2


def test_plates_stocks_error_recorded_not_fatal():
    adapter = _make_adapter(
        plates_by_type={PLATE_TYPE_CONCEPT: [
            {"plate_code": "C1", "plate_name": "芯片"},
            {"plate_code": "C2", "plate_name": "军工"}]},
        stocks_by_key={(PLATE_TYPE_CONCEPT, "C1"): [
            {"stock_code": "000001", "time_in": "2020-01-01", "time_out": ""}]},
        rank_by_type={},
    )
    orig = adapter._client.plates_stocks.side_effect

    def _flaky(plate_type, plate_code, date):
        if plate_code == "C2":
            raise ConnectionError("boom")
        return orig(plate_type, plate_code, date)

    adapter._client.plates_stocks.side_effect = _flaky
    out = adapter._get_concept_plates({"date": "2026-08-21", "sleep_s": 0})
    assert len(out["records"]) == 1
    assert any("C2" in e for e in out["stats"]["errors"])


# ---------------------------------------------------------------------------
# 管道: 宇宙过滤 / 写库语义 / fail loud
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE concept_daily (code TEXT, date TEXT, concept_name TEXT, "
            "change_pct REAL, source TEXT DEFAULT '', "
            "PRIMARY KEY (code, date, concept_name))")
        self._conn.execute("CREATE TABLE stock_info (code TEXT, industry TEXT)")
        self._conn.commit()

    def get_connection(self):
        return self._conn


def _pipeline_with_records(db, records, stats=None):
    pipe = ConceptPlatesPipeline(db=db)
    pipe._fetch_plates = lambda params: (
        {"records": records, "stats": stats or {}}, "zzshare")
    return pipe


def test_pipeline_filters_to_stock_info_universe_and_counts():
    """统筹附加②: 宇宙外记录过滤 + 计数 (341 事件探针)。"""
    db = _FakeDB()
    db.get_connection().executemany(
        "INSERT INTO stock_info (code) VALUES (?)", [("000001",), ("000002",)])
    db.get_connection().commit()
    records = [
        {"code": "000001", "concept_name": "芯片", "source": "zzshare_concept_15", "change_pct": 0.5},
        {"code": "999999", "concept_name": "芯片", "source": "zzshare_concept_15", "change_pct": 0.5},
    ]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21"))
    assert result.status == "success"
    assert result.records_written == 1
    assert result.metadata["filtered_out"] == 1
    rows = db.get_connection().execute("SELECT code FROM concept_daily").fetchall()
    assert rows == [("000001",)]


def test_pipeline_empty_universe_refuses_write():
    """stock_info 宇宙为空 → 拒绝写库 (341 事件教训)。"""
    db = _FakeDB()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15", "change_pct": 0.5}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21"))
    assert result.status == "failed"
    assert result.records_written == 0
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM concept_daily").fetchone()[0] == 0


def test_pipeline_writes_null_change_pct_not_zero():
    db = _FakeDB()
    db.get_connection().execute("INSERT INTO stock_info (code) VALUES ('000001')")
    db.get_connection().commit()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15", "change_pct": None}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21"))
    assert result.status == "success"
    row = db.get_connection().execute(
        "SELECT change_pct FROM concept_daily").fetchone()
    assert row[0] is None


def test_pipeline_deletes_industry_fallback_for_date():
    """真概念到场 → 该日行业兜底行退场 (行业不再冒充概念)。"""
    db = _FakeDB()
    conn = db.get_connection()
    conn.execute("INSERT INTO stock_info (code) VALUES ('000001')")
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001', '2026-08-21', '半导体', 0.0, 'industry_fallback')")
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001', '2026-08-20', '半导体', 0.0, 'industry_fallback')")
    conn.commit()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15", "change_pct": 0.5}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21"))
    assert result.status == "success"
    assert result.metadata["deleted_fallback_rows"] == 1
    rows = conn.execute(
        "SELECT date, concept_name FROM concept_daily ORDER BY date").fetchall()
    # 08-20 的兜底行不动, 08-21 换成真概念
    assert rows == [("2026-08-20", "半导体"), ("2026-08-21", "芯片")]


def test_pipeline_zero_records_fails_loud():
    """移交A/DL-26 教训: 拉取 0 行必须 fail loud, 不静默写空。"""
    db = _FakeDB()
    pipe = ConceptPlatesPipeline(db=db)
    pipe._fetch_plates = lambda params: ({"records": [], "stats": {}}, "zzshare")
    result = pipe.execute(PipelineInput(date="2026-08-21"))
    assert result.status == "failed"
    assert result.errors


def test_pipeline_metadata_reports_write_lock_hold():
    """GO-7 三数之一: 写锁持有时间落 metadata。"""
    db = _FakeDB()
    db.get_connection().execute("INSERT INTO stock_info (code) VALUES ('000001')")
    db.get_connection().commit()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15", "change_pct": 0.5}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21"))
    assert "write_lock_hold_s" in result.metadata
    assert result.metadata["write_lock_hold_s"] >= 0


def test_pipeline_backfill_config_marks_source():
    db = _FakeDB()
    db.get_connection().execute("INSERT INTO stock_info (code) VALUES ('000001')")
    db.get_connection().commit()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15" + BACKFILL_SOURCE_SUFFIX,
                "change_pct": 0.5}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-11", config={"backfill": True}))
    assert result.metadata["backfill"] is True
    row = db.get_connection().execute("SELECT source FROM concept_daily").fetchone()
    assert row[0].endswith(BACKFILL_SOURCE_SUFFIX)


def test_pipeline_dry_run_writes_nothing():
    """DL-32 基类拦截: dry_run 目标表行数不变。"""
    db = _FakeDB()
    db.get_connection().execute("INSERT INTO stock_info (code) VALUES ('000001')")
    db.get_connection().commit()
    records = [{"code": "000001", "concept_name": "芯片",
                "source": "zzshare_concept_15", "change_pct": 0.5}]
    result = _pipeline_with_records(db, records).execute(
        PipelineInput(date="2026-08-21", dry_run=True))
    assert result.metadata.get("dry_run") is True
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM concept_daily").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# GO-7 硬闸: 注册进 PipelineRegistry, 但不接进 orchestrator sub_pipelines
# ---------------------------------------------------------------------------

def test_go7_gate_not_in_orchestrator_sub_pipelines():
    """🔴 GO-7: concept_plates 不得出现在 orchestrator 的 sub_pipelines 列表,
    直到写锁血栓排期落地 (日管道持写锁 22-40min 撞崩 factor_update)。"""
    orch = (_root / "src" / "ops_layer" / "orchestrator.py").read_text(encoding="utf-8")
    # 抓 sub_pipelines = [...] 字面量
    import re
    # DL-51 重构后 sub_pipelines 改为模块级常量 DAILY_SUB_PIPELINES
    m = re.search(r"DAILY_SUB_PIPELINES\s*=\s*\[(.*?)\]", orch, re.S)
    assert m, "orchestrator DAILY_SUB_PIPELINES 常量未找到"
    assert "concept_plates" not in m.group(1)


def test_registered_in_data_layer():
    """管道必须注册进 PipelineRegistry (可被 registry.run 单独调起)。"""
    init = (_root / "src" / "data_layer" / "__init__.py").read_text(encoding="utf-8")
    assert "ConceptPlatesPipeline" in init
