"""审计: 减仓数量必须按 100 股整手 (仅清仓允许零股)。"""
from unittest.mock import MagicMock

from execution_layer.core.rebalance_engine import RebalanceEngine


def _engine(qty, monkeypatch, max_pct=20.0):
    pool = MagicMock()
    pool.get.return_value = {"position_qty": qty}
    eng = RebalanceEngine(MagicMock(), pool)
    monkeypatch.setattr(eng, "_effective_max_single_pct", lambda: max_pct)
    monkeypatch.setattr(eng, "get_regime_label", lambda: "test")
    return eng


def test_reduce_qty_rounded_up_to_board_lot(monkeypatch):
    # 持仓 3000 股 @10 = 30000, 总资金 100000 → 30% > 20%; 目标 2000 股
    # 价格 10.3 → 目标 int(20000/10.3)=1941 → 原实现减 1059 股 (非整手)
    eng = _engine(3000, monkeypatch)
    r = eng.check_reduce("600000", 10.3, 100000)
    assert r.can_reduce and r.reduce_qty % 100 == 0
    assert r.reduce_qty == 1100
    assert (3000 - r.reduce_qty) * 10.3 <= 20000


def test_reduce_never_exceeds_position(monkeypatch):
    eng = _engine(150, monkeypatch, max_pct=1.0)
    r = eng.check_reduce("600000", 100.0, 100000)
    assert r.reduce_qty <= 150
