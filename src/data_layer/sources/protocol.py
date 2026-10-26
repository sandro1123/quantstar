"""数据源协议 — Data Layer 内部使用"""

from typing import Any, Protocol

# 数据类型常量
DATA_TYPES = {
    "kline": "日K线数据",
    "kline_bulk": "按日全市场批量K线 (注册表可见降级能力, DL-25)",
    "valuation": "估值数据 (PE/PB/PS)",
    "financial": "财务数据 (EPS/ROE/营收)",
    "moneyflow": "资金流向",
    "stock_list": "股票列表",
    "hot_stock": "热榜",
    "anomaly": "异动",
    "limit_up": "涨停榜",
    "realtime_quote": "实时行情",
    "minute_data": "分钟数据",
    "tick_data": "逐笔数据",
    "news": "新闻",
    "concept": "概念板块",
    # 卡#20 adata 接入 (sm-64aaee3df477431b): 情绪族三型 + 风险档案
    "lhb": "龙虎榜数据",
    "popularity": "人气榜 (东财, b案单腿)",
    "concept_hot": "热门概念 (同花顺 20)",
    "mine_clearance": "个股风险档案 (解禁/风险扫描)",
    "industry": "行业分类",
}


class DataSource(Protocol):
    """数据源协议 — 所有源必须实现"""

    @property
    def name(self) -> str:
        """源名称，唯一标识"""
        ...

    @property
    def capabilities(self) -> set[str]:
        """声明能提供的数据类型"""
        ...

    def is_available(self) -> bool:
        """当前是否可用（检查API key、网络等）"""
        ...

    def get_data(self, data_type: str, params: dict) -> Any:
        """获取指定类型的数据"""
        ...
