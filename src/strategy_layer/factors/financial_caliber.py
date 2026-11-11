"""多口径财务数据 — TTM/FY/Q 三段口径转换

口径说明:
  - Q: 单季度数据 (Quarterly)
  - FY: 财政年度数据 (Fiscal Year, 即 Q4 年报)
  - TTM: 滚动12个月 (Trailing Twelve Months, 最近4个季度之和)
"""
import re

from common.logging import get_logger

logger = get_logger("data.financial_caliber")

FINANCIAL_METRICS = ["eps", "roe", "revenue", "profit", "gross_margin", "net_margin",
                     "total_assets", "total_liabilities", "net_assets", "operating_cash_flow"]

# 累计值指标（需要差分得到单季值）
CUMULATIVE_METRICS = {"revenue", "profit", "operating_cash_flow"}


def _parse_financial_date(date_str: str) -> tuple[int, int]:
    """解析财务日期，返回 (year, quarter)

    "202604" → (2026, 2)  Q2 2026
    "2026-03-31" → (2026, 1)  Q1 2026
    "2025-12-31" → (2025, 4)  FY 2025
    """
    if re.match(r"^\d{6}$", date_str):  # YYYYMM
        year = int(date_str[:4])
        month = int(date_str[4:6])
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):  # YYYY-MM-DD
        year = int(date_str[:4])
        month = int(date_str[5:7])
    else:
        return (0, 0)
    quarter = (month - 1) // 3 + 1
    return (year, quarter)


def _is_annual_report(year: int, quarter: int) -> bool:
    """Q4 报告按财政年度"""
    return quarter == 4


def _quarter_to_month(year: int, quarter: int) -> int:
    return (quarter - 1) * 3 + 3  # Q1→3, Q2→6, Q3→9, Q4→12


def _last_n_quarters(year: int, quarter: int, n: int = 4) -> list[tuple[int, int]]:
    """获取前 N 个季度的 (year, quarter) 列表"""
    result = []
    for i in range(n):
        q = quarter - i
        y = year
        while q < 1:
            q += 4
            y -= 1
        result.append((y, q))
    return result


def _diff_cumulative(q_current: float, q_prev: float | None) -> float:
    """累计值差分：当前累计值 - 上一季度累计值 = 单季值

    Q1 没有上季度数据时，当前值本身就是单季值
    """
    if q_current is None:
        return 0.0
    if q_prev is None:
        return float(q_current)
    return float(q_current) - float(q_prev)


def get_quarterly_value(financials: list[dict], metric: str,
                         year: int, quarter: int) -> float:
    """获取单季度值

    Args:
        financials: 财务数据列表（按日期排序）
        metric: 指标名
        year, quarter: 目标年份和季度
    Returns:
        单季度值
    """
    f"{year}{quarter * 3:02d}"
    current = None
    prev = None

    for fin in reversed(financials):
        fy, fq = _parse_financial_date(fin.get("date", ""))
        if fy == year and fq == quarter:
            current = fin.get(metric, 0)
        elif fy == year and fq == quarter - 1 or fy == year - 1 and fq == 4 and quarter == 1:
            prev = fin.get(metric, 0)

    if current is None:
        return 0.0

    if metric in CUMULATIVE_METRICS:
        if quarter == 1:
            return float(current or 0)
        return _diff_cumulative(current, prev)
    return float(current or 0)


def get_ttm_value(financials: list[dict], metric: str,
                    year: int, quarter: int) -> float:
    """获取 TTM 值（最近4个季度之和）

    Args:
        financials: 财务数据列表
        metric: 指标名
        year, quarter: 目标年份和季度
    Returns:
        TTM 值
    """
    total = 0.0
    for y, q in _last_n_quarters(year, quarter, 4):
        total += get_quarterly_value(financials, metric, y, q)
    return total


def get_fy_value(financials: list[dict], metric: str, year: int) -> float:
    """获取财政年度值（Q4 年报值）

    Args:
        financials: 财务数据列表
        metric: 指标名
        year: 目标年份
    Returns:
        年度值
    """
    for fin in reversed(financials):
        fy, fq = _parse_financial_date(fin.get("date", ""))
        if fy == year and fq == 4:
            return float(fin.get(metric, 0) or 0)
    return 0.0


def get_financial_caliber(financials: list[dict], metric: str,
                           year: int, quarter: int,
                           caliber: str = "Q") -> float:
    """统一口径获取财务数据

    Args:
        financials: 财务数据列表
        metric: 指标名
        year, quarter: 目标年份和季度
        caliber: 口径 "Q" | "FY" | "TTM"
    Returns:
        对应口径的值
    """
    if caliber == "TTM":
        return get_ttm_value(financials, metric, year, quarter)
    elif caliber == "FY":
        return get_fy_value(financials, metric, year)
    else:
        return get_quarterly_value(financials, metric, year, quarter)


def get_latest_financial_date(financials: list[dict]) -> tuple[int, int]:
    """获取最新的财务数据日期"""
    if not financials:
        return (0, 0)
    latest = financials[-1]
    return _parse_financial_date(latest.get("date", ""))


def get_financial_data_for_factor(financials: list[dict], metric: str,
                                    caliber: str = "TTM") -> float:
    """为因子计算获取财务数据（自动选择最新可用日期）

    Args:
        financials: 财务数据列表
        metric: 指标名
        caliber: 口径
    Returns:
        财务数据值
    """
    year, quarter = get_latest_financial_date(financials)
    if year == 0:
        return 0.0
    return get_financial_caliber(financials, metric, year, quarter, caliber)