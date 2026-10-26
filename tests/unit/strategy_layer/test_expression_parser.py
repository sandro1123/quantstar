"""Tests for expression parser and evaluator"""
import pytest
from strategy_layer.expression.parser import tokenize, parse, evaluate, Token, ASTNode


class TestTokenizer:
    def test_tokenize_number(self):
        tokens = tokenize("123")
        assert len(tokens) == 1
        assert tokens[0].type == "NUMBER"
        assert tokens[0].value == "123"

    def test_tokenize_ident(self):
        tokens = tokenize("close")
        assert len(tokens) == 1
        assert tokens[0].type == "IDENT"

    def test_tokenize_simple_expr(self):
        tokens = tokenize("close + 1")
        assert len(tokens) == 3

    def test_tokenize_raises_on_invalid(self):
        with pytest.raises(SyntaxError):
            tokenize("@invalid")

    def test_tokenize_function_call(self):
        tokens = tokenize("ts_sum(close, 5)")
        assert len(tokens) == 6
        assert tokens[0].value == "ts_sum"
        assert tokens[0].type == "IDENT"


class TestParser:
    def test_parse_number(self):
        ast = parse(tokenize("42"))
        assert ast.type == "number"
        assert ast.value == 42.0

    def test_parse_ident(self):
        ast = parse(tokenize("close"))
        assert ast.type == "ident"
        assert ast.value == "close"

    def test_parse_binary_op(self):
        ast = parse(tokenize("a + b"))
        assert ast.type == "binary_op"
        assert ast.value == "+"

    def test_parse_nested(self):
        ast = parse(tokenize("(a + b) * c"))
        assert ast.type == "binary_op"
        assert ast.value == "*"

    def test_parse_unary_minus(self):
        ast = parse(tokenize("-x"))
        assert ast.type == "unary_op"
        assert ast.value == "-"

    def test_parse_ternary(self):
        ast = parse(tokenize("a > 0 ? b : c"))
        assert ast.type == "ternary"

    def test_parse_function_call(self):
        ast = parse(tokenize("ts_sum(close, 5)"))
        assert ast.type == "call"
        assert ast.value == "ts_sum"
        assert len(ast.children) == 2


class TestEvaluator:
    def test_evaluate_number(self):
        ast = parse(tokenize("42"))
        assert evaluate(ast, {}) == 42.0

    def test_evaluate_ident(self):
        ast = parse(tokenize("close"))
        assert evaluate(ast, {"close": 10.5}) == 10.5

    def test_evaluate_binary_op(self):
        ast = parse(tokenize("a + b"))
        assert evaluate(ast, {"a": 3, "b": 4}) == 7.0

    def test_evaluate_comparison(self):
        ast = parse(tokenize("a > b"))
        assert evaluate(ast, {"a": 5, "b": 3}) == 1.0
        assert evaluate(ast, {"a": 1, "b": 3}) == 0.0

    def test_evaluate_ternary(self):
        ast = parse(tokenize("a > 0 ? 10 : 20"))
        assert evaluate(ast, {"a": 1}) == 10.0
        assert evaluate(ast, {"a": 0}) == 20.0

    def test_evaluate_unary_minus(self):
        ast = parse(tokenize("-x"))
        assert evaluate(ast, {"x": 5}) == -5.0

    def test_evaluate_function_call(self):
        ast = parse(tokenize("clip(5, 0, 3)"))
        result = evaluate(ast, {})
        assert result == 3.0

    def test_evaluate_division_by_zero(self):
        ast = parse(tokenize("a / 0"))
        assert evaluate(ast, {"a": 5}) == 0.0