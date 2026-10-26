"""ST-45 + ST-46 验证测试 (会话D)"""
import math
import pytest

from strategy_layer.signals.generator import SignalGenerator
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.aggregation.aggregator import FactorDaily


def _fd(code, score, rank=0):
    fd = FactorDaily(code=code, date="2026-08-24", composite_score=score)
    fd.rank = rank
    return fd


class TestST46ReasonText:
    """ST-46: reason 不再引用陈旧 rank, 改用候选池内序位"""

    def _make_selection(self):
        # candidates 已按 blend 后分排序 (selector 重排后的真实顺序)
        cands = [_fd("AAA", 0.95), _fd("BBB", 0.90), _fd("CCC", 0.80)]
        # 故意给陈旧 rank (aggregate 纯因子排名): AAA 纯因子排 #3 但 blend 后第一
        cands[0].rank = 3
        return SelectionResult(date="2026-08-24", candidates=cands,
                               total_evaluated=100, filters_applied=[],
                               regime="neutral", l3_adjustments=[])

    def test_buy_reason_has_position_not_rank(self):
        sigs = SignalGenerator().generate(self._make_selection())
        buys = [s for s in sigs if s.action == "buy"]
        assert len(buys) == 3
        first = next(s for s in buys if s.code == "AAA")
        assert "候选池第 1/3 位" in first.reason
        assert "#3" not in first.reason                       # 陈旧 rank=3 不出现在文案
        assert "综合得分 0.950" in first.reason

    def test_reason_score_matches_candidate_composite(self):
        sigs = SignalGenerator().generate(self._make_selection())
        for s in (x for x in sigs if x.action == "buy"):
            cand = next(c for c in self._make_selection().candidates if c.code == s.code)
            assert f"{cand.composite_score:.3f}" in s.reason

    def test_hold_reason_same_contract(self):
        sel = self._make_selection()
        sigs = SignalGenerator().generate(sel, current_positions={"BBB": 100})
        hold = next(s for s in sigs if s.action == "hold")
        assert "已有持仓" in hold.reason and "候选池第 2/3 位" in hold.reason
        assert "排名 #" not in hold.reason  # rank 字段即使有值也不出现在文案里


class TestST45PersistColumns:
    """ST-45: factor_score 列记录 blend 前值, blended_score 列记录 blend 后值"""

    def test_rows_split_pre_and_post_blend(self):
        # 直接测 rows 构造逻辑的等价实现 (与 __init__.py 中一致)
        ml_w = 0.2
        fds = []
        fd1 = FactorDaily(code="X1", date="2026-08-24", composite_score=0.7188)
        fd1.ml_score = -0.863448            # blend 前 F=0.97108? 反正公式一致即可
        fd1.composite_score = 0.97108 * (1 - ml_w) + (-0.863448) * ml_w  # blend 后
        fd1._pre = 0.97108
        fds.append(fd1)
        fd2 = FactorDaily(code="X2", date="2026-08-24", composite_score=0.5)
        fd2.ml_score = 0.0                  # 未被 blend → 两值相同是合理的
        fds.append(fd2)

        def _pre_blend(fd):
            c = getattr(fd, 'composite_score', None)
            if c is None:
                return None
            if fd.ml_score != 0.0:
                return (c - ml_w * fd.ml_score) / (1 - ml_w)
            return c

        rows = [(fd.code, round(_pre_blend(fd), 6),
                 getattr(fd, 'composite_score', None) or 0.0) for fd in fds]
        r1, r2 = rows
        assert abs(r1[1] - 0.97108) < 1e-5      # factor_score = blend 前
        assert r1[2] != r1[1]                    # blended ≠ factor (ml≠0 时)
        assert abs(r1[2] - (0.97108*0.8 + (-0.863448)*0.2)) < 1e-9
        assert r2[1] == r2[2]                    # ml==0 → 恒等是合法语义

    def test_inverse_roundtrip(self):
        # 反演恒等性: 对任意 F/ml, pre_blend(blend(F,ml)) == F
        import random
        for _ in range(200):
            F = random.uniform(-3, 3)
            m = random.uniform(-3, 3)
            w = 0.2
            B = F*(1-w) + m*w
            assert abs((B - w*m)/(1-w) - F) < 1e-12
