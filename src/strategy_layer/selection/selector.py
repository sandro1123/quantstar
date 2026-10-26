"""选股管道 — L1 过滤 + L2 Top-N + L3 市场状态调整"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.calendar import TradingCalendar
from common.logging import get_logger
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.expression.filter_dsl import FilterDSL

logger = get_logger("strategy.selection")


def _normalize_date(raw: str) -> str:
    """list_date 归一化为 YYYY-MM-DD (库内 YYYYMMDD/YYYY-MM-DD 混杂); 无法解析返回 ''"""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return ""


@dataclass
class SelectionResult:
    date: str
    candidates: list[FactorDaily] = field(default_factory=list)
    total_evaluated: int = 0
    filters_applied: list[str] = field(default_factory=list)
    regime: str = "neutral"
    l3_adjustments: list[str] = field(default_factory=list)

class SelectionPipeline:
    def __init__(self, config: dict | None = None, filter_dsl: FilterDSL | None = None,
                 data_layer=None):
        self.config = config or {}
        self.filter_dsl = filter_dsl
        self.data_layer = data_layer
        # 交易日历 (次新股"上市≥60交易日"判定); 有 db 时用实际交易日, 否则周末+节假日近似
        self._calendar = TradingCalendar(db=getattr(data_layer, "db", None) if data_layer else None)

    def _cfg(self, key: str, default):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _l1_hard_filters(self, candidates: list[FactorDaily], date: str,
                         filters: list[str]) -> list[FactorDaily]:
        """ST-03: L1 硬过滤 — 股价 / 日均成交额 / 停牌 / 次新股。

        阈值走 config (strategy.selection.l1.*): min_price=2 元, min_amount=5000 万,
        amount_window=20 日, min_days_listed=60 交易日。
        停牌判据为近似 ("当日全市场有 kline 而该股无"); 精确判据待 DL-17 增强。
        次新股: list_date 未知 → 拒绝 (fail-closed, 与 DL-04 同哲学);
        当前 stock_info.list_date 覆盖仅 3.3%, 待 DL-21 回填后此过滤才真正可用。
        """
        min_price = float(self._cfg("strategy.selection.l1.min_price", 2.0))
        min_amount = float(self._cfg("strategy.selection.l1.min_amount", 50_000_000.0))
        amount_window = int(self._cfg("strategy.selection.l1.amount_window", 20))
        min_days_listed = int(self._cfg("strategy.selection.l1.min_days_listed", 60))
        # 趋势闸门 (默认开): 过滤 MA5<MA10<MA20 空头排列 — 与 exit layer4 对称口径,
        # 从源头堵住"反转因子选出的票入场次日即被技术退出全灭" (2026-08-28 模拟盘首日事故)
        trend_gate = bool(self._cfg("strategy.selection.l1.trend_gate", True))

        # 批量取近 N 日 kline (股价/成交额/停牌判定共用)
        klines_by_code: dict[str, list] = {}
        trading_days: list[str] = []
        if self.data_layer is not None and date:
            start = (datetime.strptime(date, "%Y-%m-%d")
                     - timedelta(days=amount_window * 2)).strftime("%Y-%m-%d")
            try:
                snaps = self.data_layer.snapshot_range(start, date) or []
                for snap in snaps:
                    for k in (getattr(snap, "klines", None) or []):
                        klines_by_code.setdefault(k.code, []).append(k)
                trading_days = sorted({k.date for ks in klines_by_code.values() for k in ks})
            except Exception as e:
                logger.warning(f"L1 硬过滤: kline 获取失败, 股价/成交额/停牌过滤跳过: {e}")

        # list_date (次新股过滤)
        list_dates: dict[str, str] = {}
        if self.data_layer is not None:
            for c in candidates:
                try:
                    info = self.data_layer.get_stock_info(c.code)
                    list_dates[c.code] = _normalize_date(getattr(info, "list_date", "") if info else "")
                except Exception:
                    list_dates[c.code] = ""

        kept = []
        n_price = n_amount = n_trend = n_susp = n_new = n_unknown = 0
        market_traded = date in trading_days
        for c in candidates:
            ks = klines_by_code.get(c.code, [])
            today_rows = [k for k in ks if k.date == date]
            # 股价过滤 (当日收盘价)
            if today_rows and (today_rows[0].close or 0) < min_price:
                n_price += 1
                continue
            # 日均成交额过滤 (近 N 日)
            if ks:
                recent = sorted(ks, key=lambda k: k.date)[-amount_window:]
                avg_amount = sum(getattr(k, "amount", 0) or 0 for k in recent) / len(recent)
                if avg_amount < min_amount:
                    n_amount += 1
                    continue
            # 趋势闸门: 空头排列 (MA5<MA10<MA20) 过滤; kline 不足 20 根 fail-open 放行
            if trend_gate and ks:
                recent20 = sorted(ks, key=lambda k: k.date)[-20:]
                if len(recent20) >= 20:
                    closes = [getattr(k, "close", 0) or 0 for k in recent20]
                    ma5 = sum(closes[-5:]) / 5
                    ma10 = sum(closes[-10:]) / 10
                    ma20 = sum(closes) / 20
                    if ma5 < ma10 < ma20:
                        n_trend += 1
                        continue
            # 停牌过滤: 当日全市场有 kline 而该股无 (近似判据, 精确版待 DL-17)
            if market_traded and not today_rows:
                n_susp += 1
                continue
            # 次新股过滤: 上市 < 60 交易日; 上市日期未知 → 拒绝 (待 DL-21 回填 list_date)
            ld = list_dates.get(c.code, "")
            if not ld:
                n_unknown += 1
                continue
            try:
                days_listed = self._calendar.trading_days_between(ld, date)
            except Exception:
                n_unknown += 1
                continue
            if days_listed < min_days_listed:
                n_new += 1
                continue
            kept.append(c)

        filters.append(f"L1 股价<{min_price}元: {n_price} 只")
        filters.append(f"L1 日均成交额<{min_amount / 1e8:.1f}亿: {n_amount} 只")
        filters.append(f"L1 趋势闸门(空头排列): {n_trend} 只")
        filters.append(f"L1 停牌(当日无kline): {n_susp} 只")
        filters.append(f"L1 次新<{min_days_listed}交易日: {n_new} 只")
        filters.append(f"L1 上市日期未知(拒绝, 待DL-21回填): {n_unknown} 只")
        return kept

    def run(self, factor_daily: list[FactorDaily], market_regime: str = "neutral",
            excluded_codes: set[str] | None = None) -> SelectionResult:
        candidates = list(factor_daily)
        filters = []
        l3_adjustments = []
        total = len(candidates)
        date = factor_daily[0].date if factor_daily else ""

        # L0: 声明式筛选 DSL
        if self.filter_dsl is not None:
            before = len(candidates)
            ctx = {c.code: {"composite_score": c.composite_score} for c in candidates}
            result = self.filter_dsl.apply(ctx)
            candidates = [c for c in candidates if c.code in result.passed]
            if len(candidates) < before:
                filters.append(f"DSL 筛选: {before - len(candidates)} 只被过滤")

        # L1: 过滤 ST / 退市 / 风险代码
        # 旧实现按 code 前缀判断 ("ST"/"*ST"), 但 code 是纯 6 位数字, 该判断恒为假 (死代码)。
        # 现改为由调用方传入 excluded_codes (data_layer.get_risky_codes(), 按 stock_info.name
        # 匹配 ST/退 + is_active=0)。excluded_codes 为 None/空集时不过滤 (fail-open)。
        before = len(candidates)
        candidates = [c for c in candidates if c.code]
        if excluded_codes:
            candidates = [c for c in candidates if c.code not in excluded_codes]
        if len(candidates) < before:
            filters.append(f"排除 ST/退市: {before - len(candidates)} 只")

        # L1b: ST-03 硬过滤 (股价/成交额/停牌/次新股)
        if self.data_layer is not None and candidates:
            candidates = self._l1_hard_filters(candidates, date, filters)

        # L2: Top-N (根据市场状态调整，在 L3.1 之前确定 top_n)
        top_n_map = self.config.get("strategy.selection.top_n", {"bull": 36, "neutral": 15, "bear": 5})
        top_n = top_n_map.get(market_regime, 15)
        candidates = candidates[:top_n]
        filters.append(f"Top {top_n} (regime={market_regime})")

        # L3: 动态调整
        # L3.1: 熊市收缩
        if market_regime == "bear":
            keep = max(1, int(len(candidates) * 0.5))
            candidates = candidates[:keep]
            l3_adjustments.append(f"熊市调整: 候选减少至 {keep} 只")

        # L3.2: 行业去重 (跳过无行业数据的情况)
        sector_limit = self.config.get("strategy.selection.sector_limit", 3)
        seen_sectors: dict[str, list[FactorDaily]] = {}
        deduped = []
        has_sector_data = any(c.sector for c in candidates)
        if has_sector_data:
            for c in candidates:
                sector = c.sector or "unknown"
                if sector not in seen_sectors:
                    seen_sectors[sector] = []
                if len(seen_sectors[sector]) < sector_limit:
                    seen_sectors[sector].append(c)
                    deduped.append(c)
            removed = len(candidates) - len(deduped)
            if removed > 0:
                l3_adjustments.append(f"行业去重: 每行业最多 {sector_limit} 只, 移除 {removed} 只")
            candidates = deduped
        else:
            l3_adjustments.append("行业去重: 无行业数据，跳过")

        # L3.3: 近期表现反馈 (幅度感知)
        # 口径铁律 (2026-10-26 审计 S-1): 只许读 recent_return = T-N → T **已实现**收益
        # (由 Aggregator.fill_recent_returns 从 data_layer.realized_return 填)。
        # forward_return (T → T+N) 是未来量, 出现在这里就是前视泄漏 —— 绝不允许。
        adjusted = 0
        for c in candidates:
            if c.recent_return is None:
                continue
            val = c.recent_return
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val):
                continue  # 脏值不参与排序 (NaN 会让 sort 结果不确定)
            boost = max(-0.2, min(0.2, val * 2))
            if boost != 0:
                from copy import copy
                idx = candidates.index(c)
                c = copy(c)
                c.composite_score *= (1 + boost)
                candidates[idx] = c
                adjusted += 1
        if adjusted > 0:
            l3_adjustments.append(f"近期表现调整: {adjusted} 只股票评分已调整")

        # 重新排序 (评分调整后)
        candidates.sort(key=lambda x: x.composite_score, reverse=True)

        logger.info(f"Selection: {total} → {len(candidates)} candidates")
        return SelectionResult(
            date=factor_daily[0].date if factor_daily else "",
            candidates=candidates,
            total_evaluated=total,
            filters_applied=filters,
            regime=market_regime,
            l3_adjustments=l3_adjustments,
        )
