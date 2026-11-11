# ruff: noqa: F401
"""ST-STYLE 盘口因子族 — 中短线/强势股风格 (统筹派单 sm-25d60e49f5e046d3)

四族 (数据全在库, 零新采集):
  ①涨停基因族 (limit_up_dna): limit_up_ladder — 连板高度 (board_num)
  ②竞价强度族 (auction_strength): auction_snapshot — 竞价涨幅 (auction_pct)
     预热期契约: 竞价历史不可回补, 当日无该股竞价行 → None (案A 语义, 不造假 0)
  ③隔夜收益族 (overnight_gap): daily_klines — 昨收→今开 gap
  ④情绪代理族 (market_emotion_proxy): 市场级涨停梯队高度 (七连板+比例)
"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


def _f(x, default=None):
    """安全 float — None/非数 → default。"""
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


class LimitUpDnaFactor(BaseFactor):
    """①涨停基因 — 近30日连板高度峰值 (ladder 天梯语义: 30日连板矩阵)。

    表设计: snapshot_date=快照日, trade_date 可在 30 日窗内任意日 —
    基因 = "近窗曾到 N 连板" 而非 "今日恰涨停" (冒烟 003032 实证:
    曾到 3 板后断板, trade_date≠当日, 按当日口径漏判)。
    """
    name = "limit_up_dna"
    description = "涨停基因: 近30日连板高度峰值 (2=首板次日, N=N连板)"
    category = "style"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        rows = (ctx.shortterm_data or {}).get("ladder_rows", [])
        best = None  # (board_num, row)
        for r in rows:
            if r.get("code") != ctx.code:
                continue
            bn = _f(r.get("board_num"))
            if bn is not None and bn >= 2 and (best is None or bn > best[0]):
                best = (bn, r)
        if best is None:
            # 近窗从无连板 = 无涨停基因 → None (非 0, 区分"没涨停"与"微涨")
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        bn, r = best
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=float(bn - 2), raw_value=float(bn),
            details={"board_level": r.get("board_level"), "board_num": int(bn),
                     "peak_trade_date": r.get("trade_date")},
        )


class AuctionStrengthFactor(BaseFactor):
    """②竞价强度 — 当日竞价涨幅 (预热期诚实 None)。"""
    name = "auction_strength"
    description = "竞价强度: 集合竞价涨幅% (竞价历史不可回补, 缺=诚实缺席)"
    category = "style"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        rows = (ctx.shortterm_data or {}).get("auction_rows", [])
        for r in rows:
            if r.get("code") == ctx.code:
                ap = _f(r.get("auction_pct"))
                if ap is not None:
                    return FactorResult(
                        code=ctx.code, date=ctx.date, factor_name=self.name,
                        score=round(ap / 100.0, 6), raw_value=ap,
                        details={"auction_pct": ap},
                    )
                break
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)


class OvernightGapFactor(BaseFactor):
    """③隔夜收益 — 昨收→今开 gap (全史可算)。"""
    name = "overnight_gap"
    description = "隔夜收益: (今开-昨收)/昨收"
    category = "style"
    window_size = 2

    def compute(self, ctx: FactorContext) -> FactorResult:
        ks = ctx.klines
        if len(ks) < 2:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        def _g(k, f):
            try:
                return float(k.get(f) if isinstance(k, dict) else getattr(k, f))
            except (TypeError, ValueError, AttributeError):
                return None
        pre = _g(ks[-2], "close")
        op = _g(ks[-1], "open")
        if not pre or op is None or pre <= 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        gap = (op - pre) / pre
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(gap, 6), raw_value=round(gap * 100, 4),
            details={"pre_close": pre, "open": op},
        )


class MarketEmotionProxyFactor(BaseFactor):
    """④情绪代理 — 市场级涨停梯队高度 (每股权重相同)。"""
    name = "market_emotion_proxy"
    description = "市场情绪代理: 当日涨停梯队最高连板高度 (情绪温度)"
    category = "style"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        v = (ctx.shortterm_data or {}).get("max_board_num")
        if v is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=float(v), raw_value=float(v),
            details={"max_board_num": v},
        )
