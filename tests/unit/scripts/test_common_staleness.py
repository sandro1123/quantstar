"""_common.staleness_report 时点感知 — factor_ic 的 T−10 设计滞后不得报假红。

病 (F席 E6 侦察 2026-09-16): factor_ic.calc_date = 最新 kline 日 − 10 交易日
(strategy.ic_horizon=10, **设计**; 源码锚 src/ops_layer/orchestrator.py:2292
_resolve_ic_target_date)。旧 STALE_WATCH 把它当普通表: MAX(calc_date) < asof
即报「停在 X」→ 每次日报/周报水印恒假红, 信号失去分辨力。

同族先例: src/ops_layer/readiness.py:29-30 — adjust_factors 故意不入清单
(「需时点感知期望值, 另出提案」)。本卡即该提案在 marimo 页侧的落地。

判据: 表可声明「合法滞后交易日数」(默认 0); 期望日 = asof 回退 N 个交易日。
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


def _patch_max(monkeypatch, mapping):
    """monkeypatch C.one: 按表名返回其 MAX(日期列) 值; 未指定表按 asof 在位。"""
    base = {"index_daily": _ASOF, "lhb_daily": _ASOF,
            "sector_fund_flow": _ASOF, "factor_ic": _ASOF}
    base.update(mapping)

    def fake_one(sql, params=()):
        for tbl, mx in base.items():
            if tbl in sql:
                return (mx,)
        return (_ASOF,)
    monkeypatch.setattr(C, "one", fake_one)


def test_factor_ic_lag_not_reported_stale(monkeypatch):
    """factor_ic 落在 T−10 设计滞后内 → 不报 stale (假红消除)。

    生产实况: asof=2026-09-15, factor_ic MAX(calc_date)=2026-09-01
    (= 09-15 回退 10 个交易日, 合法设计值)。
    """
    _patch_max(monkeypatch, {"factor_ic": "2026-09-01"})
    stale = C.staleness_report(_ASOF)
    assert not any("factor_ic" in s for s in stale), \
        f"factor_ic 误报假红 (T−10 设计滞后被当病): {stale}"


def test_factor_ic_beyond_lag_still_reported(monkeypatch):
    """factor_ic 超出 T−10 合法滞后 → 仍报 (分辨力保留, 防假绿)。"""
    _patch_max(monkeypatch, {"factor_ic": "2026-08-20"})
    stale = C.staleness_report(_ASOF)
    assert any("factor_ic" in s for s in stale), \
        f"factor_ic 真滞后未报 (假绿): {stale}"


def test_real_stale_tables_still_reported(monkeypatch):
    """非滞后表 (默认滞后 0) 真滞后仍报 — 时点感知不豁免普通表。"""
    _patch_max(monkeypatch, {"index_daily": "2026-09-14"})
    stale = C.staleness_report(_ASOF)
    assert any("index_daily" in s for s in stale), \
        f"index_daily 真滞后未报: {stale}"


def test_note_declares_lag_semantics(monkeypatch):
    """factor_ic 备注须声明 T−10 滞后语义 (语义载入文本, 非措辞断言)。"""
    note = C.STALE_WATCH["factor_ic"][1]
    assert "10" in note and "滞" in note, \
        f"备注未声明 T−10 滞后语义: {note!r}"
