"""估值预计算层 — 从原始数据计算常用估值指标"""

from dataclasses import dataclass


@dataclass
class ValuationMetrics:
    """预计算估值指标"""
    code: str
    date: str
    pe_ttm: float = 0.0
    pb: float = 0.0
    ps_ttm: float = 0.0
    ev_ebitda: float = 0.0
    fcf_yield: float = 0.0
    dividend_yield: float = 0.0
    market_cap: float = 0.0
    pe_lyr: float = 0.0


class ValuationCalculator:
    """从基础数据计算估值指标 (纯计算，无DB依赖)"""

    def calculate(self, code: str, date: str,
                  price: float = 0.0, eps: float = 0.0,
                  bvps: float = 0.0, revenue_per_share: float = 0.0,
                  market_cap: float = 0.0, total_shares: float = 0.0,
                  ebitda: float = 0.0, fcf: float = 0.0,
                  dividend: float = 0.0, net_debt: float = 0.0) -> ValuationMetrics:
        """计算所有估值指标"""

        return ValuationMetrics(
            code=code,
            date=date,
            pe_ttm=round(price / eps, 2) if eps != 0 else 0.0,
            pb=round(price / bvps, 2) if bvps != 0 else 0.0,
            ps_ttm=round(price / revenue_per_share, 2) if revenue_per_share != 0 else 0.0,
            ev_ebitda=round((market_cap + net_debt) / ebitda, 2) if ebitda != 0 else 0.0,
            fcf_yield=round(fcf / market_cap, 4) if market_cap != 0 else 0.0,
            dividend_yield=round(dividend / price, 4) if price != 0 else 0.0,
            market_cap=market_cap,
            pe_lyr=round(price / eps, 2) if eps != 0 else 0.0,
        )
