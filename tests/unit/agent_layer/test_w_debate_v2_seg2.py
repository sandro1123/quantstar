# -*- coding: utf-8 -*-
"""AG-DEBATE-V2 第二段 — RED 先行 (:memory:, 无网络)。

派单 sm-21793763 两件:
  ① invalidation_conditions 消费面: expert 失效条件 → 执行层条件单四件套格式映射
     (EX-4 advise/order_plan 下游自家闭环)
  ② deep_debate rounds=2 生效干跑
"""
import sqlite3
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from agent_layer.debate.models import ExpertReport  # noqa: E402
from agent_layer.advisor import invalidation_bridge as ib  # noqa: E402 ← RED: 模块不存在


class _DB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE stock_pool (
                code TEXT PRIMARY KEY, name TEXT, status TEXT,
                source TEXT, added_date TEXT, initial_score REAL,
                latest_signal_date TEXT, entry_low REAL, entry_high REAL,
                valid_until TEXT, current_score REAL, last_price REAL,
                watch_reason TEXT, exit_reason TEXT, exit_reason_detail TEXT,
                exit_layer INTEGER, updated_at TEXT)""")
        self._conn.execute("""
            CREATE TABLE agent_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT,
                code TEXT, verdict TEXT, confidence REAL,
                created_at TEXT, reasoning TEXT)""")
        self._conn.commit()

    def get_connection(self):
        return self._conn


@pytest.fixture
def db():
    return _DB()


def _seed(conn, code, **kw):
    cols = {"code": code, "name": code, "status": "pending_entry",
            "source": "t", "added_date": "2026-09-12",
            "initial_score": 0.6, "entry_low": 10.0, "entry_high": 10.5,
            "valid_until": "2099-01-01", "last_price": 10.2}
    cols.update(kw)
    keys, ph = ",".join(cols), ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO stock_pool ({keys}) VALUES ({ph})", tuple(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# ① invalidation_conditions → 执行层条件单映射
# ---------------------------------------------------------------------------

def test_bridge_maps_conditions_into_advise_pass_reason(db):
    """① 失效条件经桥接进 advise 的 pass 面: risk 否决外新增 invalidation 否决。

    映射规则: expert 失效条件含「跌破 X」价格线且 X 高于条件单止损 → 该票
    advise 输出附 invalidation_warnings (执行层可读格式), 不改三态判定
    (失效条件=警示非否决 — 观察单, 供报告/复盘面)。
    """
    _seed(db.get_connection(), "000001", entry_low=10.0, entry_high=10.5,
          last_price=10.2)
    reports = [ExpertReport(expert_name="tech", score=0.5, key_points=["均线多头"],
                            confidence=0.7,
                            invalidation_conditions=["跌破 9.5 支撑线", "量能萎缩 50%"])]
    out = ib.advise_with_invalidation(db, "000001", last_price=10.2,
                                      today="2026-09-12",
                                      expert_reports=reports)
    # 三态判定不被失效条件翻面 (仍 buy_now — 失效条件是警示层)
    assert out["state"] == "buy_now"
    # 失效条件以执行层可读格式附出 (结构化, 非原文本堆)
    warns = out.get("invalidation_warnings") or []
    assert isinstance(warns, list) and len(warns) >= 1
    assert any("9.5" in str(w) for w in warns)


def test_bridge_no_conditions_no_warnings(db):
    """专家报告无失效条件 → 无 warnings 键值 (零扰动)。"""
    _seed(db.get_connection(), "000002", last_price=10.2)
    reports = [ExpertReport(expert_name="tech", score=0.5, key_points=["k"],
                            confidence=0.7)]
    out = ib.advise_with_invalidation(db, "000002", last_price=10.2,
                                      today="2026-09-12",
                                      expert_reports=reports)
    assert out["state"] == "buy_now"
    assert not (out.get("invalidation_warnings") or [])


def test_bridge_conditions_json_serializable(db):
    """① 输出 JSON 契约 (报告面可序列化)。"""
    import json
    _seed(db.get_connection(), "000003", last_price=10.2)
    reports = [ExpertReport(expert_name="risk", score=-0.2, key_points=["k"],
                            confidence=0.7,
                            invalidation_conditions=["跌破 10 日线"])]
    out = ib.advise_with_invalidation(db, "000003", last_price=10.2,
                                      today="2026-09-12",
                                      expert_reports=reports)
    s = json.dumps(out, ensure_ascii=False, default=str)
    assert "invalidation" in s


def test_bridge_aggregates_across_experts(db):
    """多专家失效条件聚合去重 (tech+risk 各一条 → 桥接面合并)。"""
    _seed(db.get_connection(), "000004", last_price=10.2)
    reports = [
        ExpertReport(expert_name="tech", score=0.4, key_points=["k"], confidence=0.6,
                    invalidation_conditions=["跌破 9.5 支撑"]),
        ExpertReport(expert_name="risk", score=-0.2, key_points=["k"], confidence=0.6,
                    invalidation_conditions=["放量跌破 9.5 支撑", "公告反转"]),
    ]
    out = ib.advise_with_invalidation(db, "000004", last_price=10.2,
                                      today="2026-09-12",
                                      expert_reports=reports)
    warns = out.get("invalidation_warnings") or []
    assert len(warns) >= 2       # 跨专家聚合


# ---------------------------------------------------------------------------
# ② rounds=2 生效干跑 (真 _run_debate 链)
# ---------------------------------------------------------------------------

def test_deep_debate_rounds2_effective_dry_run():
    """② 干跑: main 树 deep_debate 链 rounds=2 真生效 — 第 2 轮真跑且
    裁决面吃 round2。轻形态: 编排核直接对拍 (无 LLM, 规则腿)。"""
    from agent_layer.debate.rounds_orchestrator import run_debate, rounds_config

    calls = []

    class _P:
        name = "p"
        def set_memory_db(self, db): pass
        def analyze(self, data):
            calls.append(dict(data))
            return ExpertReport(expert_name=self.name, score=0.3,
                               key_points=[f"r{len(calls)}"], confidence=0.5)

    out = run_debate(
        base_data={"code": "X", "date": "d", "data_summary": "s",
                   "factor_scores": {}},
        expert_classes=[_P], config={"debate_rounds": 2},
        provider_manager=None)
    assert len(calls) == 2
    assert "peer_review" in calls[1]
    assert out["rounds_run"] == 2
    assert len(out["round2_reports"]) == 1


def test_deep_debate_config_from_yaml_dry_run():
    """② config 键从 agent.yaml 真读到 2 (出厂值) — 装载链干跑。"""
    import yaml
    cfg_path = _root / "config" / "defaults" / "agent.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from agent_layer.debate.rounds_orchestrator import rounds_config
    assert rounds_config(cfg.get("agent", {})) == 2
