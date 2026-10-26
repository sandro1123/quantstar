"""测试声明式筛选 DSL"""
import pytest
from strategy_layer.expression.filter_dsl import FilterDSL, FilterCondition, FilterResult


class TestFilterCondition:
    def test_basic_condition(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close > 1", "股价 > 1元")
        contexts = {
            "000001": {"close": 5.0},
            "000002": {"close": 0.5},
        }
        result = dsl.apply(contexts)
        assert result.passed == ["000001"]
        assert result.failed == ["000002"]

    def test_multiple_conditions(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close > 1", "股价 > 1元")
        dsl.add_condition_from_str("volume > 100000", "成交量 > 10万股")
        contexts = {
            "000001": {"close": 5.0, "volume": 200000},
            "000002": {"close": 5.0, "volume": 50000},
            "000003": {"close": 0.5, "volume": 200000},
        }
        result = dsl.apply(contexts)
        assert result.passed == ["000001"]
        assert result.failed == ["000002", "000003"]

    def test_default_stock_filters(self):
        dsl = FilterDSL.default_stock_filters()
        contexts = {
            "000001": {"composite_score": 0.8},
            "000002": {"composite_score": -0.1},
            "000003": {"composite_score": 0.0},
            "000004": {"composite_score": 2.5},
        }
        result = dsl.apply(contexts)
        assert "000001" in result.passed
        assert "000002" in result.failed
        assert "000003" in result.failed
        assert "000004" in result.passed

    def test_empty_context(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close > 1")
        result = dsl.apply({})
        assert result.passed == []
        assert result.failed == []

    def test_disabled_condition(self):
        dsl = FilterDSL()
        cond = FilterCondition(expression="close > 1", description="股价 > 1元", enabled=False)
        dsl.add_condition(cond)
        contexts = {"000001": {"close": 0.5}}
        result = dsl.apply(contexts)
        assert result.passed == ["000001"]
        assert result.failed == []

    def test_reasons(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close > 1", "股价 > 1元")
        contexts = {"000001": {"close": 0.5}}
        result = dsl.apply(contexts)
        assert "000001" in result.reasons
        assert "股价 > 1元" in result.reasons["000001"]

    def test_less_than(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("pe < 50", "PE < 50")
        contexts = {
            "000001": {"pe": 30},
            "000002": {"pe": 100},
        }
        result = dsl.apply(contexts)
        assert result.passed == ["000001"]
        assert result.failed == ["000002"]

    def test_comparison_ge(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close >= 10", "close >= 10")
        contexts = {
            "000001": {"close": 10},
            "000002": {"close": 9},
            "000003": {"close": 11},
        }
        result = dsl.apply(contexts)
        assert result.passed == ["000001", "000003"]
        assert result.failed == ["000002"]

    def test_expression_with_arithmetic(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close + volume > 100", "复合条件")
        contexts = {
            "000001": {"close": 50, "volume": 60},
            "000002": {"close": 10, "volume": 20},
        }
        result = dsl.apply(contexts)
        assert result.passed == ["000001"]
        assert result.failed == ["000002"]


class TestFilterDSLIntegration:
    def test_filter_before_l1_selector(self):
        dsl = FilterDSL()
        dsl.add_condition_from_str("close > 1", "股价 > 1元")
        contexts = {
            "000001": {"close": 5.0},
            "000002": {"close": 0.5},
        }
        result = dsl.apply(contexts)
        # 000001 passes, 000002 fails
        assert "000001" in result.passed
        assert "000002" in result.failed
        assert len(result.passed) == 1