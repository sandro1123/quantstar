# -*- coding: utf-8 -*-
"""S-1 收益语义分离钉 — recent_return(已实现) vs forward_return(未来)。

病 (外部审计 S-1, PR#4 只修了一半; 全量日期与证据见 CHANGELOG):
  旧: `fill_recent_returns` 把 T→T+N **前向**收益写进 `recent_return`,
      选股 L3.3 (`selector.py`) 拿它乘 composite_score → 历史回放用未来收益排序。
  中间态 (PR#4): 值被挪进 `forward_return`, 但 L3.3 仍读 `recent_return`
      → 字段恒 None → L3.3 静默变死代码 = 把功能连同病一起删掉 (两个错互为镜像)。

本文件钉四件事, 每条都配反向注入 (判例: 断言须能证明自己会红):
  钉1  loader.realized_return 只读 T-N→T, 且 T 当日无 K 线必须 None (不拿上一日冒充)
  钉2  fill_recent_returns 两条字段各填各的, 互不顶替
  钉3  选股 L3.3 只吃已实现收益 (forward_return 不得影响排序)
  钉4  三态边界: 行数不足 / close<=0 → None (不造假 0)

夹具日期全用 today-N 动态生成 (记忆判例: 夹具日期必须动态, 硬编码夹具会被
test_no_hardcoded_dates 机治钉咬住, 且下次换日期要改一堆文件)。
"""
from datetime import date, timedelta

import pytest

from common.database import Database
from strategy_layer.aggregation.aggregator import Aggregator, FactorDaily
from strategy_layer.selection.selector import SelectionPipeline

from tests.unit._mutation import assert_mutation_caught


def _d(offset: int) -> str:
    """相对今天的 ISO 日期 (负=过去)。不硬编码年份 → 机治钉天然不咬。"""
    return (date.today() + timedelta(days=offset)).strftime("%Y-%m-%d")


def _db_with_klines(rows):
    """rows: [(code, date, close), ...] → 建 daily_klines 并插入。"""
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT NOT NULL, date TEXT NOT NULL,
            open REAL DEFAULT 0, high REAL DEFAULT 0, low REAL DEFAULT 0,
            close REAL DEFAULT 0, volume REAL DEFAULT 0, amount REAL DEFAULT 0,
            pre_close REAL DEFAULT 0, change_pct REAL DEFAULT 0,
            turnover_rate REAL DEFAULT 0, adjust_type TEXT DEFAULT 'raw',
            PRIMARY KEY (code, date)
        )
    """)
    conn.executemany("INSERT INTO daily_klines (code, date, close) VALUES (?,?,?)", rows)
    conn.commit()
    return db


class _Loader:
    """只带 realized_return 的最小 loader 替身 — 直接复用真实现, 不 mock。"""

    def __init__(self, db):
        from data_layer._loader import DataLoader as _L
        self._impl = _L(db)

    def realized_return(self, code, date, horizon=5):
        return self._impl.realized_return(code, date, horizon)


# 5 个交易日: 100 → 110 (已实现 +10%)
_SEQ = [("600000", _d(-5), 100.0),
        ("600000", _d(-4), 102.0),
        ("600000", _d(-3), 104.0),
        ("600000", _d(-2), 106.0),
        ("600000", _d(-1), 110.0)]
_T = _d(-1)          # 决策日 = 最后一根 K 线
_T_NEXT = _d(0)      # 次日 (无 K 线)
_T_FUTURE = _d(1)    # 未来行


class TestRealizedReturnOnlyLooksBack:
    def test_realized_return_is_t_minus_n_to_t(self):
        """钉1: T 与 4 个交易日前相比 = +10%。"""
        loader = _Loader(_db_with_klines(_SEQ))
        got = loader.realized_return("600000", _T, 4)
        assert got == pytest.approx(10.0), f"应为 +10%, 实为 {got}"

    def test_realized_return_does_not_use_future_rows(self):
        """钉1 反向: 追加未来行 (close=200) 后, 同一天的已实现收益必须不变。

        若实现误按 forward_return 口径取未来价, 这里会变成 +100% → 红。
        """
        rows = _SEQ + [("600000", _T_FUTURE, 200.0)]
        loader = _Loader(_db_with_klines(rows))
        got = loader.realized_return("600000", _T, 4)
        assert got == pytest.approx(10.0), (
            f"未来行参与了计算 → 前视泄漏; 实为 {got}")

    def test_no_bar_on_date_returns_none(self):
        """钉1 边界: 当日无 K 线 (未开盘/停牌) → None, 绝不拿上一交易日冒充当日。"""
        loader = _Loader(_db_with_klines(_SEQ))
        assert loader.realized_return("600000", _T_NEXT, 4) is None
        assert loader.realized_return("600000", _T_FUTURE, 4) is None


class TestRealizedReturnThreeStates:
    def test_insufficient_history_returns_none(self):
        """钉4a: 只有 2 根 K 线却要 horizon=4 → None (次新股, 不造假)。"""
        loader = _Loader(_db_with_klines(_SEQ[:2]))
        assert loader.realized_return("600000", _d(-4), 4) is None

    def test_zero_or_negative_close_returns_none(self):
        """钉4b: 起点 close<=0 (F-23 停牌脏行) → None, 不产出 -100%。"""
        rows = [("600000", _d(-5), 0.0)] + _SEQ[1:]
        loader = _Loader(_db_with_klines(rows))
        assert loader.realized_return("600000", _T, 4) is None


class _DualLayer:
    """同时提供两条收益的假 data_layer, 值刻意相反 (已实现 +10 / 未来 -40)。"""

    def __init__(self, realized=10.0, forward=-40.0):
        self._realized = realized
        self._forward = forward

    def realized_return(self, code, dt, horizon=5):
        return self._realized

    def forward_return(self, code, dt, horizon=5):
        return self._forward


class TestFillSeparatesTwoSemantics:
    def test_recent_gets_realized_forward_gets_forward(self):
        """钉2: 两字段各填各的, 值相反也必须各就各位。"""
        fds = [FactorDaily(code="600000", date=_T, composite_score=1.0)]
        n = Aggregator.fill_recent_returns(fds, _DualLayer(), _T)
        assert fds[0].recent_return == pytest.approx(10.0), (
            "recent_return 必须是已实现收益 (选股可用), 不得是前向收益")
        assert fds[0].forward_return == pytest.approx(-40.0), (
            "forward_return 必须是前向收益 (仅 IC)")
        assert n == 1, "返回值应等于 forward_return 填充条数 (保持 PR#4 语义)"

    def test_fields_never_alias_each_other(self):
        """钉2 变异验证: 若实现让两字段互相顶替, 本断言必红。"""
        fds = [FactorDaily(code="600000", date=_T, composite_score=1.0)]

        def _check():
            assert fds[0].recent_return != fds[0].forward_return, "两字段被写成同一个值"

        Aggregator.fill_recent_returns(fds, _DualLayer(), _T)
        _check()  # 正常路径应通过

        def _mutate():
            fds[0].forward_return = fds[0].recent_return   # 注入: 前向顶替已实现

        assert_mutation_caught(mutate=_mutate, check=_check, what="两字段分离断言")

    def test_missing_realized_api_does_not_crash(self):
        """钉2 兼容: 只有 forward_return 的老 data_layer 不得崩 (逐字段能力探测)。"""
        class _FwdOnly:
            def forward_return(self, code, dt, horizon=5):
                return -40.0

        fds = [FactorDaily(code="600000", date=_T, composite_score=1.0)]
        n = Aggregator.fill_recent_returns(fds, _FwdOnly(), _T)
        assert n == 1
        assert fds[0].forward_return == pytest.approx(-40.0)
        assert fds[0].recent_return is None, "无 realized API 时应留 None, 不得拿前向顶上"


def _fd(code, score, recent=None, forward=None):
    return FactorDaily(code=code, date=_T, composite_score=score,
                       factor_scores={}, recent_return=recent, forward_return=forward)


_CFG = {"strategy.selection.top_n": {"bull": 5, "neutral": 5, "bear": 5}}


class TestSelectorOnlyReadsRealized:
    def test_l33_boosts_by_realized_return(self):
        """钉3: recent_return=+10% → composite 乘 (1+0.2) 且被记录进 l3_adjustments。"""
        res = SelectionPipeline(config=_CFG).run([_fd("600000", 1.0, recent=10.0)],
                                                 market_regime="neutral")
        assert res.candidates[0].composite_score == pytest.approx(1.2)
        assert any("近期表现调整" in x for x in res.l3_adjustments)

    def test_l33_ignores_forward_return(self):
        """钉3 核心: 只给 forward_return (未来量) 时, L3.3 必须完全不动作。

        反向: 若 L3.3 误读 forward_return, composite 会被乘 0.2 → 红。
        """
        res = SelectionPipeline(config=_CFG).run(
            [_fd("600000", 1.0, recent=None, forward=-40.0)], market_regime="neutral")
        assert res.candidates[0].composite_score == pytest.approx(1.0), (
            "forward_return (T→T+N) 影响了选股排序 = 前视泄漏")
        assert not any("近期表现调整" in x for x in res.l3_adjustments)

    def test_forward_return_does_not_change_ranking(self):
        """钉3 端到端: 两只票唯一差别是 forward_return → 排序结果必须一致。"""
        a = _fd("600000", 0.5, recent=0.0, forward=100.0)
        b = _fd("600001", 0.9, recent=0.0, forward=-100.0)
        res = SelectionPipeline(config=_CFG).run([a, b], market_regime="neutral")
        assert [c.code for c in res.candidates] == ["600001", "600000"], (
            f"排序被 forward_return 扭曲: {[c.code for c in res.candidates]}")

    def test_l33_survives_nan_and_non_numeric(self):
        """钉3 鲁棒: NaN/inf/字符串脏值不得参与排序, 也不得让 run 崩。"""
        fds = [_fd("600000", 1.0, recent=float("nan")),
               _fd("600001", 1.0, recent=float("inf")),
               _fd("600002", 1.0, recent="坏值")]
        res = SelectionPipeline(config=_CFG).run(fds, market_regime="neutral")
        assert len(res.candidates) == 3
        assert all(c.composite_score == pytest.approx(1.0) for c in res.candidates)
        assert not any("近期表现调整" in x for x in res.l3_adjustments)

    def test_l33_mutation_caught(self):
        """钉3 变异验证: 把 L3.3 的取值源换成 forward_return, 断言必红。"""
        fd = _fd("600000", 1.0, recent=None, forward=-40.0)

        def _check():
            res = SelectionPipeline(config=_CFG).run([fd], market_regime="neutral")
            assert res.candidates[0].composite_score == pytest.approx(1.0)

        def _mutate():
            fd.recent_return = fd.forward_return   # 注入: 让选股看到未来收益

        assert_mutation_caught(mutate=_mutate, check=_check, what="L3.3 只读已实现收益")
