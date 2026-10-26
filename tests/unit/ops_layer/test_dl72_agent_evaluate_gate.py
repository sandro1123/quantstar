"""DL-72(a): agent_evaluate 排程代码写但不接线 — 门禁测试。

统筹派发 §4: 周一 08-24 GO-7 首验 (cron 写锁守卫第一次工作日实跑),
一次只动一个变量。排程定义 (ops.yaml) 现在写, crontab 触发行等首验过了再接。

本测试钉住三件事:
1. ops.yaml 有 agent_evaluate 定义 (排程代码已落地);
2. agent_evaluate 不在 orchestrator sub_pipelines (不是日管道子任务);
3. agent_evaluate 不在 deploy/quantstar-crontab (未接线, 等 GO-7 后)。

接线时 (GO-7 首验过后): 往 deploy/quantstar-crontab 加触发行 + 应用 crontab,
并把 test_not_in_crontab 改为 test_in_crontab (翻转断言, 不是删测试)。
"""
import re
from pathlib import Path

import yaml

_root = Path(__file__).resolve().parents[3]


def test_ops_yaml_has_agent_evaluate_definition():
    """排程定义已落地: ops.yaml 有 agent_evaluate, 依赖 fwd_return_backfill。"""
    d = yaml.safe_load(
        (_root / "config" / "defaults" / "ops.yaml").read_text(encoding="utf-8"))
    cfg = d["ops"]["pipelines"].get("agent_evaluate")
    assert cfg is not None, "ops.yaml 缺 agent_evaluate 定义 (DL-72a 排程未落地)"
    assert cfg["schedule"] == "16:52"  # 9a7bf6d: 16:35->16:52 避开撞时
    assert "fwd_return_backfill" in cfg["depends_on"], (
        "agent_evaluate 消费 fwd_return_5d, 必须排在 fwd_return_backfill 之后")


def test_handler_registered_in_dispatch():
    """handler 已注册: orchestrator dispatch 有 agent_evaluate (代码存在)。"""
    orch = (_root / "src" / "ops_layer" / "orchestrator.py").read_text(encoding="utf-8")
    assert '"agent_evaluate": self._run_agent_evaluate' in orch


def test_not_in_daily_sub_pipelines():
    """agent_evaluate 不得出现在 daily_data_pipeline 的 sub_pipelines 列表。

    它是独立的更新型管线 (回写 agent_predictions 两列), 不是采集子任务。
    e002cc8 后 sub_pipelines 由模块级常量 DAILY_SUB_PIPELINES 构造
    (list(DAILY_SUB_PIPELINES) + 条件追加), 断言改查常量定义。
    """
    orch = (_root / "src" / "ops_layer" / "orchestrator.py").read_text(encoding="utf-8")
    m = re.search(r"DAILY_SUB_PIPELINES\s*=\s*\[(.*?)\]", orch, re.S)
    assert m, "orchestrator DAILY_SUB_PIPELINES 常量未找到"
    assert "agent_evaluate" not in m.group(1)
    mc = re.search(r"DAILY_SUB_PIPELINES_CONDITIONAL\s*=\s*\[(.*?)\]", orch, re.S)
    assert mc, "orchestrator DAILY_SUB_PIPELINES_CONDITIONAL 常量未找到"
    assert "agent_evaluate" not in mc.group(1)


def test_not_in_crontab_artifact():
    """🔴 未接线闸门: deploy/quantstar-crontab 不得有 agent_evaluate 触发行。

    GO-7 首验 (08-24 周一) 过了才接线; 接线时翻转本测试断言。
    """
    cron = (_root / "deploy" / "quantstar-crontab").read_text(encoding="utf-8")
    active = [l for l in cron.splitlines()
              if "agent_evaluate" in l and not l.lstrip().startswith("#")]
    assert not active, (
        f"agent_evaluate 已出现在 crontab 工件 (未过 GO-7 首验不得接线): {active}")
