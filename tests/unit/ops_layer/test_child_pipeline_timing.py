"""轨道②: ddp 子管道计时埋点 (S) + fail-fast v1 (M)。

sched-fix-design-D-2026-08-26 §3.4 / §4.4:
- ①计时埋点: per-sub-pipeline start/end 事件落 logs/child_pipeline_timing.jsonl
  (含 duration_s/records_written), 补铁律#12 盲区 — 此前 pipeline_runs 只记顶层
  ddp duration, 11+2 子管道无独立计时, 36.6min 无法归因 (瘦身前置, 先测量再瘦身)。
- ②fail-fast v1: best-effort 子管道降级 (超时/失败/0行) 时父管道不再静默 success,
  升级 partial (复用 DL-43 三态: pipeline_runs.status='partial' + warning 告警,
  can_run 对 partial 放行 → 下游不阻塞)。治 Q-51 静默失效。

事件名 child_pipeline_start / child_pipeline_end — 独立命名, 不占用 D flock
等待方案预留的 waited_lock/acquired_after_wait/wait_timeout (统筹防撞裁定)。

红-绿: 撤回 orchestrator 改动时本文件必须失败。
负向对照: 全绿路径不受影响 (不误伤); 当日 0 行且源未发布 (benign) 不降级。
"""
import json
import os
from types import SimpleNamespace

import pytest

from common.database import Database
from common.utils import bj_date
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

# DDP c-1 (#81): analyst_forecasts 拆出日管道 (16:55 独立顶层) —
# 不再是 DDP 子管道, 计时事件面同步收缩 (10+2)。
ALL_SUBS = {"stock_list", "kline", "fuyao_financial", "valuation", "moneyflow",
            "concept_daily", "lhb_daily", "market_sentiment",
            "stock_news", "stock_limit_up_down", "shortterm_collect", "index_daily"}


@pytest.fixture(autouse=True)
def _redirect_timing_jsonl(monkeypatch, tmp_path):
    """全部测试把子管道计时 jsonl 重定向到临时文件 — 避免写进生产 logs/
    (worktree 的 logs 是 symlink 指向主 checkout 生产目录)。"""
    monkeypatch.setenv("QS_CHILD_PIPELINE_TIMING", str(tmp_path / "child_timing.jsonl"))


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    # moneyflow 0 行探针需要表存在 (best-effort 0 行降级测试)
    conn.execute("CREATE TABLE IF NOT EXISTS stock_moneyflow (code TEXT, date TEXT)")
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


def _fake_app(db, run_pipeline):
    data = SimpleNamespace(db=db, run_pipeline=run_pipeline, pipelines=_FakeRegistry(ALL_SUBS))
    return SimpleNamespace(data=data, execution=None)


def _read_events(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- ① 计时埋点 ---

def test_child_pipeline_timing_events_emitted(monkeypatch, tmp_path):
    """每个子管道产生一对 start/end 事件, end 含 duration_s + records_written。"""
    jsonl = str(tmp_path / "timing.jsonl")
    monkeypatch.setenv("QS_CHILD_PIPELINE_TIMING", jsonl)
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=5)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    events = _read_events(jsonl)
    starts = [e for e in events if e["event"] == "child_pipeline_start"]
    ends = [e for e in events if e["event"] == "child_pipeline_end"]
    assert len(starts) == len(ALL_SUBS), "每个子管道应有 start 事件"
    assert len(ends) == len(ALL_SUBS), "每个子管道应有 end 事件"
    # end 事件必须含计时与行数 (瘦身归因的核心字段)
    for e in ends:
        assert "duration_s" in e and isinstance(e["duration_s"], (int, float))
        assert e["records_written"] == 5
        assert e["status"] == "success"
        assert e["date"] == "2026-08-22"
    # start/end 成对覆盖全部子管道名
    assert {e["pipeline"] for e in starts} == ALL_SUBS
    assert {e["pipeline"] for e in ends} == ALL_SUBS


def test_child_pipeline_end_records_timeout_none(monkeypatch, tmp_path):
    """子管道超时 (with_timeout 返回 None) 时 end 事件 status=None 留痕。"""
    jsonl = str(tmp_path / "timing.jsonl")
    monkeypatch.setenv("QS_CHILD_PIPELINE_TIMING", jsonl)
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        if name == "index_daily":
            return None  # 模拟 with_timeout 截断
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    ends = {e["pipeline"]: e for e in _read_events(jsonl) if e["event"] == "child_pipeline_end"}
    assert ends["index_daily"]["status"] is None
    assert ends["index_daily"]["records_written"] == 0


# --- ② fail-fast v1: best-effort 降级 → 父管道 partial ---

def test_best_effort_timeout_escalates_parent_partial(monkeypatch):
    """best-effort 子管道超时 (None) → 父管道 partial, 不再静默 success。"""
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        if name == "index_daily":
            return None
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert result.final_status == "partial", "best-effort 超时应降级父管道 partial"
    assert any("index_daily" in e for e in result.metadata.get("errors", []))


def test_best_effort_failure_escalates_parent_partial(monkeypatch):
    """best-effort 子管道报 failed → 父管道 partial。"""
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        if name == "moneyflow":
            return PipelineResult(name=name, date=pip_input.date, status="failed",
                                  errors=["source rate limited"])
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert result.final_status == "partial"
    assert any("moneyflow" in e for e in result.metadata.get("errors", []))


def test_best_effort_zero_rows_backfill_escalates_parent_partial(monkeypatch):
    """best-effort 子管道回填日 0 行 (无存量) → 父管道 partial (Q-51 静默失效)。"""
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        if name == "moneyflow":
            return PipelineResult(name=name, date=pip_input.date, status="success",
                                  records_written=0)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    # 回填日 (非当日) 0 行且无存量 → 明确降级
    result = orch._run_data_pipeline({"start_date": "2026-08-20", "end_date": "2026-08-20"})

    assert result.final_status == "partial"
    assert any("moneyflow" in e for e in result.metadata.get("errors", []))


# --- 负向对照: 不误伤 ---

def test_all_green_stays_success(monkeypatch):
    """全绿路径: 所有子管道 success 且有行 → 父管道保持 success (不误伤)。"""
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=3)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert result.final_status == "success", "全绿路径不应被 fail-fast 误伤"
    assert not result.metadata.get("partial")


def test_same_day_zero_rows_before_publication_stays_success(monkeypatch):
    """当日 0 行且无存量 (源未发布, benign) → 不降级, 父管道保持 success。"""
    db, orch = _orchestrator()
    today = bj_date()

    def run_pipeline(name, pip_input):
        if name == "moneyflow":
            return PipelineResult(name=name, date=pip_input.date, status="success",
                                  records_written=0)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": today, "end_date": today})

    assert result.final_status == "success", "源未发布的当日 0 行不应误伤为降级"


def test_core_failure_still_failed_not_partial(monkeypatch):
    """核心子管道 (kline) 失败 → 父管道保持 failed, 不被 fail-fast 降级 partial 掩盖。"""
    db, orch = _orchestrator()

    def run_pipeline(name, pip_input):
        if name == "kline":
            return PipelineResult(name=name, date=pip_input.date, status="failed",
                                  errors=["all sources empty"])
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=1)

    monkeypatch.setattr(orch, "_get_app", lambda: _fake_app(db, run_pipeline))
    monkeypatch.setattr(orch, "_min_kline_rows", lambda: 0)

    result = orch._run_data_pipeline({"start_date": "2026-08-22", "end_date": "2026-08-22"})

    assert result.final_status == "failed", "核心失败必须保持 failed, 不得被 partial 掩盖"
