"""测试 Alpha 表达式 DSL 解析器"""

import pytest
from strategy_layer.expression.parser import tokenize, parse, evaluate, Token, ASTNode


class TestTokenizer:
    def test_basic_expression(self):
        tokens = tokenize("a + b")
        assert len(tokens) == 3
        assert tokens[0] == Token("IDENT", "a", 0)
        assert tokens[1] == Token("OP", "+", 2)
        assert tokens[2] == Token("IDENT", "b", 4)

    def test_with_parentheses(self):
        tokens = tokenize("(a + b) * 2")
        types = [t.type for t in tokens]
        assert types == ["LPAREN", "IDENT", "OP", "IDENT", "RPAREN", "OP", "NUMBER"]

    def test_function_call(self):
        tokens = tokenize("ts_sum(close, 5)")
        types = [t.type for t in tokens]
        assert types == ["IDENT", "LPAREN", "IDENT", "COMMA", "NUMBER", "RPAREN"]

    def test_numbers(self):
        tokens = tokenize("3.14 + 2")
        assert tokens[0] == Token("NUMBER", "3.14", 0)
        assert tokens[2] == Token("NUMBER", "2", 7)

    def test_invalid_character(self):
        with pytest.raises(SyntaxError, match="Unexpected character"):
            tokenize("a @ b")


class TestParser:
    def test_number(self):
        ast = parse(tokenize("42"))
        assert ast.type == "number"
        assert ast.value == 42.0

    def test_ident(self):
        ast = parse(tokenize("close"))
        assert ast.type == "ident"
        assert ast.value == "close"

    def test_binary_op(self):
        ast = parse(tokenize("a + b"))
        assert ast.type == "binary_op"
        assert ast.value == "+"
        assert len(ast.children) == 2
        assert ast.children[0].type == "ident"
        assert ast.children[1].type == "ident"

    def test_parentheses(self):
        ast = parse(tokenize("(a + b)"))
        assert ast.type == "binary_op"
        assert ast.value == "+"

    def test_function_call(self):
        ast = parse(tokenize("ts_sum(close, 5)"))
        assert ast.type == "call"
        assert ast.value == "ts_sum"
        assert len(ast.children) == 2
        assert ast.children[0].type == "ident"
        assert ast.children[0].value == "close"
        assert ast.children[1].type == "number"
        assert ast.children[1].value == 5.0

    def test_empty_tokens(self):
        with pytest.raises(SyntaxError):
            parse([])


class TestEvaluate:
    def test_simple_arithmetic(self):
        ast = parse(tokenize("3 + 4"))
        assert evaluate(ast, {}) == 7.0

    def test_arithmetic_with_context(self):
        ast = parse(tokenize("a + b * 2"))
        assert evaluate(ast, {"a": 1, "b": 3}) == 7.0

    def test_division(self):
        ast = parse(tokenize("a / b"))
        assert evaluate(ast, {"a": 10, "b": 2}) == 5.0

    def test_division_by_zero(self):
        ast = parse(tokenize("a / b"))
        assert evaluate(ast, {"a": 10, "b": 0}) == 0.0

    def test_subtraction(self):
        ast = parse(tokenize("a - b"))
        assert evaluate(ast, {"a": 5, "b": 3}) == 2.0

    def test_missing_ident(self):
        ast = parse(tokenize("x + 1"))
        assert evaluate(ast, {}) == 1.0

    def test_parentheses_priority(self):
        ast = parse(tokenize("(1 + 2) * 3"))
        assert evaluate(ast, {}) == 9.0

    def test_ts_sum_call(self):
        ast = parse(tokenize("ts_sum(close, 5)"))
        context = {"close": 0}
        result = evaluate(ast, context)
        assert isinstance(result, float)

    def test_rank_call(self):
        ast = parse(tokenize("rank(values)"))
        context = {"values": 0}
        result = evaluate(ast, context)
        assert isinstance(result, float)

    def test_unknown_function(self):
        ast = parse(tokenize("nonexistent_func(x)"))
        with pytest.raises(ValueError, match="Unknown function"):
            evaluate(ast, {"x": 1})

    def test_nested_calls(self):
        ast = parse(tokenize("ts_sum(ts_sum(close, 3), 5)"))
        context = {"close": 0}
        result = evaluate(ast, context)
        assert isinstance(result, float)


class TestIntegration:
    def test_expression_roundtrip(self):
        expr = "ts_sum(close, 5) + rank(open) * 2"
        tokens = tokenize(expr)
        ast = parse(tokens)
        result = evaluate(ast, {"close": 0, "open": 0})
        assert isinstance(result, float)

    def test_complex_expression(self):
        expr = "ts_rank(close, 10) + abs_(open) / 2"
        tokens = tokenize(expr)
        ast = parse(tokens)
        args = {"close": 0, "open": -5}
        result = evaluate(ast, args)
        assert isinstance(result, float)