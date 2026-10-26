"""p2_factors 页自身陈旧判定 + _common.expected_floor — 第二处同源假红。

病 (F席 E6 侦察 09-16): p2_factors.py:38
    IC_STALE = bool(IC_DATE) and str(IC_DATE) < ASOF
与 _common.STALE_WATCH 同源假红, 但**独立实现** —— 页自己在顶部报
「⚠️ factor_ic 停在 09-01（早于数据截至日 09-15）——ic_feedback 链待查」,
**这正是统筹「E6 空转」判断的直接来源** (统筹读的就是这个页/这段文本)。

修法: 期望日下沉为 _common.expected_floor(table, asof) 单点定义,
staleness_report 与 p2 页**共用同一真源** (防两处各写一份再漂移)。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "marimo_pages"))

import _common as C  # noqa: E402

import importlib

import pytest

@pytest.fixture(autouse=True)
def _pin_ic_horizon(tmp_path, monkeypatch):
    """锚定 ic_horizon=10 (生产 local.yaml 裁定值 #108) — 防 CI/异机读不到宿主配置回落 5。

    _common 在 import 期读 QS_ROOT/config/local.yaml 建 STALE_WATCH;
    注入隔离 QS_ROOT 后 reload, 判据与宿主环境脱钩 (同族病: 测试耦合部署面)。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "local.yaml").write_text("strategy:" + chr(10) + "  ic_horizon: 10" + chr(10), encoding="utf-8")
    monkeypatch.setenv("QS_ROOT", str(tmp_path))
    importlib.reload(C)
    yield
    importlib.reload(C)  # 回落默认路径 (对后续测试无断言依赖)


_ASOF = "2026-09-15"


def test_expected_floor_factor_ic_is_t_minus_lag():
    """expected_floor('factor_ic', asof) = asof 回退 ic_horizon 交易日。"""
    floor = C.expected_floor("factor_ic", _ASOF)
    assert str(floor) == "2026-09-01", f"期望日应为 09-01, 实为 {floor}"


def test_expected_floor_plain_table_is_asof():
    """无滞后声明的表 → 期望日 = asof 当日。"""
    assert str(C.expected_floor("index_daily", _ASOF)) == _ASOF


def test_expected_floor_unknown_table_none():
    """未知表 → None (调用方保守处理, 不得静默当不陈旧)。"""
    assert C.expected_floor("no_such_table", _ASOF) is None


def test_expected_floor_bad_asof_none():
    """asof 不可解析 → None。"""
    assert C.expected_floor("factor_ic", "not-a-date") is None


def _strip_comments(src: str) -> str:
    """剥掉 # 注释与 docstring —— 反面断言须只看**代码**, 否则测的是措辞。

    (判例: 反面断言关键词若只在正面语境出现, 否则测的是措辞 —— 我本轮已自踩
     一次: 把旧表达式写进说明性注释, 字面串检测即假阳。)
    """
    import re
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_p2_page_uses_shared_source_not_naive_compare():
    """p2 页须用共享期望日, 不得再自行拿 IC_DATE 与 ASOF 裸比。"""
    raw = (_ROOT / "scripts" / "marimo_pages" / "p2_factors.py").read_text(encoding="utf-8")
    assert "expected_floor" in raw, "p2 未接共享期望日 (仍会报同源假红)"
    code = _strip_comments(raw)
    assert "IC_DATE) < ASOF" not in code, \
        "p2 代码仍在裸比 asof (假红未除; 本断言已剥注释, 非措辞检测)"
