"""多口径财报数据支持: TTM / FY / Q"""
from dataclasses import dataclass


@dataclass
class FiscalData:
    """财务数据（多口径）"""
    code: str
    date: str
    caliber: str
    revenue: float = 0.0
    profit: float = 0.0
    eps: float = 0.0
    roe: float = 0.0
    gross_margin: float = 0.0
    net_margin: float = 0.0
    total_assets: float = 0.0
    total_liabilities: float = 0.0
