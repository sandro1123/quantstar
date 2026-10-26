"""Ops Layer 集成测试 — Agent 接入"""
from unittest.mock import MagicMock, patch
from ops_layer.orchestrator import PipelineOrchestrator


def test_agent_pipeline_handlers_registered():
    """验证 Agent pipeline handlers 已注册到 dispatch"""
    dispatch = {
        "agent_evaluate": None,
        "agent_discovery": None,
        "agent_cleanup": None,
    }
    for name in dispatch:
        assert name in dispatch, f"Pipeline {name} not registered"


def test_crontab_has_agent_entries(monkeypatch):
    """部署断言须对**仓内 crontab 模板** (可移植真源), 不读宿主机 crontab (CI runner 没有)。"""
    import subprocess
    from pathlib import Path
    tmpl = (Path(__file__).resolve().parents[3] / "deploy" / "quantstar-crontab").read_text(encoding="utf-8")
    fake = subprocess.CompletedProcess(args=["crontab", "-l"], returncode=0, stdout=tmpl, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    assert "morning_brief" in result.stdout
    assert "intraday_scan" in result.stdout
    assert "daily_update" in result.stdout


def test_agent_layer_accepts_all_tasks():
    from agent_layer import AgentLayer
    from agent_layer.core.models import AgentRequest
    from unittest.mock import MagicMock

    al = AgentLayer(database=MagicMock(), config=MagicMock())
    for task in ["stock_analysis", "entry_check", "exit_review", "quick_check", "committee"]:
        req = AgentRequest(task=task, input={"code": "000001"}, consumer="test")
        resp = al.analyze(req)
        assert resp.verdict in ("推荐", "不推荐", "观望", "警惕"), f"Task {task} failed"


def test_execution_layer_accepts_agent():
    from execution_layer import ExecutionLayer
    from unittest.mock import MagicMock
    el = ExecutionLayer(db=MagicMock(), config=MagicMock(), agent_layer=MagicMock())
    assert el.agent_layer is not None
    assert el.entry_engine.agent_layer is not None
    assert el.exit_engine.agent_layer is not None