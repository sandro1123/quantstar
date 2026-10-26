"""DDP c-1 (#81 两连超时修复): analyst_forecasts 拆独立 16:55 顶层管道接线测试
(统筹 sm-6e53bd96df784290; 施工规范沿用 concept_plates_daily_wiring 同款)。

约束 (设计稿 §4c-1 语义核心):
  1. dispatch 表挂 analyst_forecasts → _run_analyst_forecasts;
  2. 拆分是外科手术式: DAILY_SUB_PIPELINES 只删 analyst_forecasts 一个成员,
     其余十名日管道成员面零变化 (DDP 串行 2738→908s 预算回归的全部来源);
  3. ops.yaml 管道定义在位 (排程准绳): schedule 16:55 (错峰 16:52
     agent_evaluate 同刻冲突) / days 1-5 / timeout_s 1900 (与 data_layer
     _capability_timeout 1900 双钉对齐) / depends_on 空;
  4. handler metadata 直传 (coverage/attempted) 不造数;
     None 结果显式失败 (08-17 analyst 60s 截断误报教训的独立路径防线)。

红-绿: 撤回 orchestrator/ops.yaml 改动时本文件必须失败。
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import DAILY_SUB_PIPELINES, PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

_root = Path(__file__).resolve().parents[3]


def _read(p):
    return p.read_text(encoding="utf-8")


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


class _FakeRegistry:
    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


def _fake_app(db, registry, run_pipeline):
    data = SimpleNamespace(db=db, run_pipeline=run_pipeline, pipelines=registry)
    return SimpleNamespace(data=data, execution=None)


# --- ① dispatch 接线 ---

def test_dispatch_wired():
    """orchestrator dispatch 挂 analyst_forecasts。"""
    orch = _read(_root / "src" / "ops_layer" / "orchestrator.py")
    assert '"analyst_forecasts": self._run_analyst_forecasts' in orch
    assert "def _run_analyst_forecasts" in orch


# --- ② 拆分语义: 外科手术式删除 ---

def test_analyst_removed_from_daily_sub_pipelines():
    """c-1 拆分: analyst_forecasts 不再是 DDP 串行成员 (预算回归来源)。"""
    subs = " ".join(DAILY_SUB_PIPELINES)
    assert "analyst_forecasts" not in subs, \
        "analyst_forecasts 已拆独立 16:55 管道, 不得再进 DAILY_SUB_PIPELINES"
    # 其余十名成员面零变化 — 拆分必须外科手术式, 不许误伤
    for keep in ("stock_list", "kline", "fuyao_financial", "valuation",
                 "moneyflow", "concept_daily", "lhb_daily", "market_sentiment",
                 "stock_news", "stock_limit_up_down"):
        assert keep in subs, f"拆分误伤: {keep} 应仍在日管道成员面"


# --- ③ ops.yaml 排程准绳 ---

def test_ops_yaml_pipeline_defined():
    """排程准绳: config/defaults/ops.yaml 管道定义四要素。"""
    y = _read(_root / "config" / "defaults" / "ops.yaml")
    m = re.search(r"^    analyst_forecasts:\n((?:      .*\n)+)", y, re.M)
    assert m, "ops.yaml 缺 analyst_forecasts 管道定义"
    body = m.group(1)
    assert 'schedule: "16:55"' in body, "窗=16:55 (错峰 16:52 agent_evaluate)"
    assert 'days: "1-5"' in body, "交易日 1-5"
    assert "timeout_s: 1900" in body, "超时 1900s (实测 ~1830s, 与 _capability_timeout 双钉)"
    assert "depends_on: []" in body, "无依赖 (独立错峰管道)"


# --- ④ handler 行为: 管线直调 + metadata 直传 ---

def test_handler_runs_pipeline_metadata_passthrough(monkeypatch):
    """handler 直调 analyst_forecasts 管线, coverage/attempted 直传不造数。"""
    db, orch = _orchestrator()
    called = []

    def run_pipeline(name, pip_input):
        called.append(name)
        return PipelineResult(name=name, date=pip_input.date, status="success",
                              records_written=4748,
                              metadata={"coverage": 0.826, "attempted": 5748})

    app = _fake_app(db, _FakeRegistry({"analyst_forecasts"}), run_pipeline)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_analyst_forecasts({})

    assert called == ["analyst_forecasts"]
    assert result.success is True
    assert result.metadata.get("analyst_records") == 4748
    assert result.metadata.get("coverage") == 0.826, "coverage 须直传管线 metadata"
    assert result.metadata.get("attempted") == 5748, "attempted 须直传管线 metadata"


def test_handler_unregistered_skips_not_fails(monkeypatch):
    """未注册: 跳过+告警, 不判失败 (铁律 #12 不静默; 缺失不阻塞)。"""
    db, orch = _orchestrator()
    app = _fake_app(db, _FakeRegistry(set()), lambda n, i: None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_analyst_forecasts({})

    assert result.success is True
    assert result.metadata.get("skipped") is True


def test_handler_none_result_reports_failure(monkeypatch):
    """管线返回 None (with_timeout 截断): 必须显式失败 — 08-17 analyst 60s
    截断误报教训的 c-1 独立路径防线 (1900s 双钉下不应发生, 发生即留痕)。"""
    db, orch = _orchestrator()
    app = _fake_app(db, _FakeRegistry({"analyst_forecasts"}), lambda n, i: None)
    monkeypatch.setattr(orch, "_get_app", lambda: app)

    result = orch._run_analyst_forecasts({})

    assert result.success is False
    assert any("空结果" in e for e in result.errors)
