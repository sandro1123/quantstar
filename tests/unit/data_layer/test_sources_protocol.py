"""测试 DataSource 协议"""

import pytest
from data_layer.sources.protocol import DataSource, DATA_TYPES


def test_data_types_complete():
    """测试数据类型常量完整"""
    required = {"kline", "valuation", "financial", "moneyflow", "stock_list"}
    assert required.issubset(DATA_TYPES.keys())


def test_data_source_protocol():
    """测试 DataSource 协议定义"""
    # 协议本身不能实例化，但可以用于类型检查
    assert hasattr(DataSource, "name")
    assert hasattr(DataSource, "capabilities")
    assert hasattr(DataSource, "is_available")
    assert hasattr(DataSource, "get_data")


class MockSource:
    """模拟数据源，用于测试协议"""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> set[str]:
        return {"kline", "valuation"}

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict):
        return {"data": "mock_data"}


def test_mock_source_implements_protocol():
    """测试 MockSource 符合协议"""
    source = MockSource()
    assert source.name == "mock"
    assert "kline" in source.capabilities
    assert source.is_available() is True
    assert source.get_data("kline", {}) == {"data": "mock_data"}
