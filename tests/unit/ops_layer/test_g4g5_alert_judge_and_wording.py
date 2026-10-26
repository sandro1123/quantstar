# -*- coding: utf-8 -*-
"""G4/G5 测试钉。

G4: 耗时基线偏离改为单侧 —— 变快不报警，只报变慢。
G5: WFO 告警措辞三态 —— 「未评估」不得说成「低于 0.5」。
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))


# ════════ G4: 耗时判据单侧 ════════

class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _detector(baseline_mean, baseline_std):
    from ops_layer.anomaly_detector import AnomalyDetector
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE metric_baselines (metric_name TEXT, metric_kind TEXT, "
        "baseline_value REAL, baseline_std REAL, needs_reset INTEGER)")
    conn.execute("INSERT INTO metric_baselines VALUES "
                 "('morning_brief', 'pipeline_duration', ?, ?, 0)",
                 (baseline_mean, baseline_std))
    conn.commit()
    det = AnomalyDetector.__new__(AnomalyDetector)
    det.db = _FakeDB(conn)
    det._baselines = {}
    return det


def test_faster_than_baseline_does_not_alert():
    """钉1 (G4 核心): 耗时显著**变快**不得报警。

    实证 #123: morning_brief 16.0s vs 基线 56.9s±11.1 => Z=-3.69
    而 16s 正是修好「60s 超时截断」后的正常结果。
    """
    det = _detector(56.9, 11.1)
    r = det.check_duration_baseline("morning_brief", 16.0)
    assert r is None, (
        f"变快被误报为异常: {r} —— 基线取自病态期时，好转不该算异常")


def test_slower_than_baseline_still_alerts():
    """钉2: 耗时显著**变慢**仍必须报警（不能一刀切关掉）。"""
    det = _detector(16.0, 3.0)
    r = det.check_duration_baseline("morning_brief", 60.0)
    assert r is not None, "变慢必须报警"
    assert r["anomaly_type"] == "baseline_deviation"
    assert "偏高" in r["detail"] or "变慢" in r["detail"]


def test_within_threshold_no_alert():
    """钉3: 在阈值内不报警（两个方向都不报）。"""
    det = _detector(30.0, 5.0)
    assert det.check_duration_baseline("morning_brief", 31.0) is None


# ════════ G5: WFO 措辞三态 ════════

class _Eval:
    def __init__(self, passed, oos_efficiency):
        self.passed = passed
        self.oos_efficiency = oos_efficiency


def test_wfo_unevaluated_distinguished_from_fail():
    """钉4 (G5 核心): oos_efficiency 为 None 时返回 hold_unevaluated，
    不得与「真低于 0.5」共用同一个 hold。"""
    from backtest_layer.feedback.wfo_gate import WFOGate

    gate = WFOGate() if callable(WFOGate) else WFOGate
    decide = None
    for name in dir(WFOGate):
        if "tier" in name.lower() or "decide" in name.lower():
            decide = getattr(WFOGate, name)
            break
    if decide is None:
        pytest.skip("未找到 tier 判定方法名")

    # None => hold_unevaluated
    r_none = decide(gate, _Eval(False, None))
    assert r_none == "hold_unevaluated", (
        f"oos_efficiency=None 应返回 hold_unevaluated, 实得 {r_none}")

    # 0.3 => hold（真不过）
    r_low = decide(gate, _Eval(False, 0.3))
    assert r_low == "hold", (
        f"oos_efficiency=0.3 应返回 hold, 实得 {r_low}")

    # 0.9 => paper_trading
    r_ok = decide(gate, _Eval(False, 0.9))
    assert r_ok == "paper_trading", f"0.9 应返回 paper_trading, 实得 {r_ok}"

    # passed => deploy
    r_pass = decide(gate, _Eval(True, None))
    assert r_pass == "deploy", f"passed 应返回 deploy, 实得 {r_pass}"


def test_wfo_alert_wording_distinguishes_unevaluated():
    """钉5: 告警措辞必须区分「未评估」与「低于 0.5」。

    (源码级钉: orchestrator 里 hold_unevaluated 分支必须给出「未评估」措辞)
    """
    import re
    path = os.path.join(ROOT, "src", "ops_layer", "orchestrator.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "hold_unevaluated" in src, (
        "orchestrator 未处理 hold_unevaluated —— 措辞仍会混用")
    # 必须在 hold_unevaluated 分支里出现「未评估」字样
    seg = src[src.index("hold_unevaluated"):]
    seg = seg[:seg.index("else:")] if "else:" in seg else seg[:800]
    assert "未评估" in seg or "从未产生" in seg, (
        f"hold_unevaluated 分支缺「未评估」诚实措辞: {seg[:200]}")


def test_wfo_alert_still_says_oversimilar_when_really_low():
    """钉6 (反向): 真低于 0.5 时，措辞仍必须说「低于 0.5」而不是「未评估」。"""
    path = os.path.join(ROOT, "src", "ops_layer", "orchestrator.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # else 分支（真 hold）里必须有「低于 0.5」
    idx = src.index("hold_unevaluated")
    tail = src[idx:idx + 1500]
    assert "低于 0.5" in tail, "真 hold 分支丢了「低于 0.5」措辞"
