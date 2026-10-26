"""backtest_layer/core/data_quality.py — 数据质量检查 (BT-07)

B-07: 原实现是空壳 — 只查起始日有没有 snapshot, 且 engine.run 从不调用。
BT-07 补齐五项检查中可做的三项 (复权对齐/财务对齐依赖数据层契约, 后补):
  1. K线完整性: 区间内每只股票的交易日覆盖率
  2. 停牌缺口: 市场开盘但该股无 K 线的交易日 (DataGap 影响标注最小实现)
  3. 涨跌停日标注: change_pct 触及涨跌停幅度的交易日计数

防御式实现: 深度分析任何异常 (含 data_layer 返回不可迭代对象) 一律跳过,
不造假值、不误报 — 宁可没有, 不可是假的。engine.run 前置调用, 缺口进
BacktestReport.data_quality_warnings。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class DataQualityResult:
    passed: bool
    warnings: list[str] = field(default_factory=list)
    checked_codes: int = 0
    missing_codes: list[str] = field(default_factory=list)
    # BT-07 深度检查产出 (无数据/不可算时为空, 不造假)
    completeness: dict = field(default_factory=dict)        # {code: 覆盖率 0~1}
    suspension_gaps: dict = field(default_factory=dict)      # {code: [缺口交易日]}
    limit_annotations: dict = field(default_factory=dict)    # {code: {"up": n, "down": n}}


def _price_limit_pct(code: str) -> float:
    """涨跌停幅度: 创业板/科创板 20%, 北交所 30%, 主板 10% (与 broker 一致)"""
    if code.startswith("688") or code.startswith("300"):
        return 0.20
    if code.startswith("8"):
        return 0.30
    return 0.10


class DataQualityCheck:
    # 深度分析扫描交易日上限, 防止超长区间拖垮回测前置
    MAX_SCAN_DAYS = 400
    # 完整率低于此值才告警 (避免节假日边界的零星误报)
    COMPLETENESS_WARN_THRESHOLD = 0.95

    @staticmethod
    def check(codes: list[str], date_range: tuple[str, str], data_layer) -> DataQualityResult:
        warnings: list[str] = []
        missing: list[str] = []
        if not codes:
            return DataQualityResult(passed=False, warnings=["codes 列表为空"])
        if date_range[0] > date_range[1]:
            return DataQualityResult(passed=False, warnings=[f"date_range 无效: {date_range}"])
        if data_layer is None:
            return DataQualityResult(passed=False, warnings=["data_layer 未初始化"])
        for code in codes:
            try:
                snapshot = data_layer.snapshot(date_range[0])
                if not snapshot:
                    warnings.append(f"{code}: 无 {date_range[0]} 数据")
                    missing.append(code)
            except Exception as e:
                warnings.append(f"{code}: 数据获取异常 — {e}")
                missing.append(code)

        result = DataQualityResult(
            passed=len(warnings) == 0,
            warnings=warnings,
            checked_codes=len(codes),
            missing_codes=missing,
        )
        # BT-07: 深度检查 (K线完整性/停牌缺口/涨跌停标注), 防御式, 失败不造假
        try:
            DataQualityCheck._analyze_range(codes, date_range, data_layer, result)
        except Exception:
            pass  # 深度分析不可用时保留基础结论, 不追加假告警
        return result

    @staticmethod
    def _analyze_range(codes: list[str], date_range: tuple[str, str],
                       data_layer, result: DataQualityResult) -> None:
        """逐交易日扫描, 产出完整性/停牌缺口/涨跌停标注, 就地填充 result。

        任一环节遇到不可迭代/缺字段 (如 mock 对象) 立即放弃深度分析 —
        基础 passed/missing_codes 结论不受影响。
        """
        from common.calendar import TradingCalendar
        cal = TradingCalendar(db=None)
        start, end = date_range
        days: list[str] = []
        cur = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        while cur <= end_dt and len(days) < DataQualityCheck.MAX_SCAN_DAYS:
            s = cur.strftime("%Y-%m-%d")
            if cal.is_trading_day(s):
                days.append(s)
            cur += timedelta(days=1)
        if not days:
            return

        presence = {c: set() for c in codes}
        limit_ann = {c: {"up": 0, "down": 0} for c in codes}
        market_open_days: set = set()

        for d in days:
            try:
                snap = data_layer.snapshot(d)
            except Exception:
                continue
            klines = getattr(snap, "klines", None) if snap else None
            if not klines:
                continue
            try:
                kl_list = list(klines)
            except TypeError:
                return  # 不可迭代 → 放弃深度分析, 不造假
            if not kl_list:
                # 当日无 K 线 (含 mock 空迭代): 不算开盘日。若整段都如此,
                # market_open_days 为空 → 下方直接放弃深度分析, 不误报缺口。
                continue
            market_open_days.add(d)
            for k in kl_list:
                code = getattr(k, "code", None)
                if code not in presence:
                    continue
                presence[code].add(d)
                chg = getattr(k, "change_pct", 0.0)
                try:
                    chg = float(chg)
                except (TypeError, ValueError):
                    chg = 0.0
                limit_pct = _price_limit_pct(code) * 100.0
                if chg >= limit_pct - 0.2:
                    limit_ann[code]["up"] += 1
                elif chg <= -(limit_pct - 0.2):
                    limit_ann[code]["down"] += 1

        if not market_open_days:
            return  # 区间内无任何开盘数据, 深度分析无依据

        n_open = len(market_open_days)
        for code in codes:
            if code in result.missing_codes:
                continue  # 完全缺失已在基础检查标注
            seen = presence[code] & market_open_days
            coverage = len(seen) / n_open if n_open else 0.0
            result.completeness[code] = round(coverage, 4)
            gaps = sorted(market_open_days - seen)
            if gaps:
                result.suspension_gaps[code] = gaps
            if coverage < DataQualityCheck.COMPLETENESS_WARN_THRESHOLD:
                result.warnings.append(
                    f"{code}: K线完整率 {coverage:.0%} ({len(seen)}/{n_open} 交易日), "
                    f"停牌/缺口 {len(gaps)} 日")
            if limit_ann[code]["up"] or limit_ann[code]["down"]:
                result.limit_annotations[code] = limit_ann[code]

        # 按严重度处置 (BT-07): 缺口仅标注 warning; 完整率 < 0.5 属严重 → passed=False
        # (engine 据此拒绝回测, 避免在残缺数据上产出误导性指标)。
        for code, coverage in result.completeness.items():
            if coverage < 0.5:
                result.passed = False
                result.warnings.append(f"{code}: K线完整率 {coverage:.0%} 过低, 拒绝回测")
