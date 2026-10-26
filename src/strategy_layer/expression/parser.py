"""Alpha 表达式解析器 — 词法分析 + 语法分析"""

import re
from dataclasses import dataclass, field
from typing import Any

TOKEN_PATTERNS = [
    ("NUMBER", r"\d+\.?\d*(?:[eE][+-]?\d+)?"),
    ("IDENT", r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("COMMA", r","),
    ("QUESTION", r"\?"),
    ("COLON", r":"),
    ("CMP", r">=|<=|!=|==|>|<"),
    ("OP", r"[+\-*/]"),
    ("WS", r"\s+"),
]


@dataclass
class Token:
    type: str
    value: str
    pos: int


@dataclass
class ASTNode:
    type: str
    value: Any = None
    children: list = field(default_factory=list)


def tokenize(expr: str) -> list[Token]:
    tokens = []
    pos = 0
    while pos < len(expr):
        match = None
        for tok_type, pattern in TOKEN_PATTERNS:
            m = re.match(pattern, expr[pos:])
            if m:
                if tok_type != "WS":
                    tokens.append(Token(tok_type, m.group(), pos))
                pos += m.end()
                match = True
                break
        if not match:
            raise SyntaxError(f"Unexpected character at position {pos}: '{expr[pos]}'")
    return tokens


def parse(tokens: list[Token]) -> ASTNode:
    pos = 0

    def peek() -> Token | None:
        return tokens[pos] if pos < len(tokens) else None

    def consume(expected_type: str | None = None) -> Token:
        nonlocal pos
        tok = tokens[pos]
        if expected_type and tok.type != expected_type:
            raise SyntaxError(f"Expected {expected_type}, got {tok.type} at {tok.pos}")
        pos += 1
        return tok

    def parse_primary() -> ASTNode:
        tok = peek()
        if not tok:
            raise SyntaxError("Unexpected end of expression")
        if tok.type == "NUMBER":
            consume("NUMBER")
            return ASTNode("number", float(tok.value))
        elif tok.type == "IDENT":
            consume("IDENT")
            if peek() and peek().type == "LPAREN":
                consume("LPAREN")
                args = []
                while peek() and peek().type != "RPAREN":
                    args.append(parse_expr())
                    if peek() and peek().type == "COMMA":
                        consume("COMMA")
                consume("RPAREN")
                return ASTNode("call", tok.value, args)
            return ASTNode("ident", tok.value)
        elif tok.type == "LPAREN":
            consume("LPAREN")
            node = parse_expr()
            consume("RPAREN")
            return node
        raise SyntaxError(f"Unexpected token {tok.type} at {tok.pos}")

    def parse_expr() -> ASTNode:
        return parse_ternary()

    def parse_ternary() -> ASTNode:
        node = parse_comparison()
        if peek() and peek().type == "QUESTION":
            consume("QUESTION")
            true_branch = parse_expr()
            consume("COLON")
            false_branch = parse_expr()
            node = ASTNode("ternary", "?:", [node, true_branch, false_branch])
        return node

    def parse_comparison() -> ASTNode:
        node = parse_add_sub()
        while peek() and peek().type == "CMP":
            op = consume("CMP")
            right = parse_add_sub()
            node = ASTNode("binary_op", op.value, [node, right])
        return node

    def parse_add_sub() -> ASTNode:
        node = parse_mul_div()
        while peek() and peek().type == "OP" and peek().value in ("+", "-"):
            op = consume("OP")
            right = parse_mul_div()
            node = ASTNode("binary_op", op.value, [node, right])
        return node

    def parse_mul_div() -> ASTNode:
        node = parse_unary()
        while peek() and peek().type == "OP" and peek().value in ("*", "/"):
            op = consume("OP")
            right = parse_unary()
            node = ASTNode("binary_op", op.value, [node, right])
        return node

    def parse_unary() -> ASTNode:
        if peek() and peek().type == "OP" and peek().value == "-":
            consume("OP")
            return ASTNode("unary_op", "-", [parse_unary()])
        if peek() and peek().type == "OP" and peek().value == "+":
            consume("OP")
            return parse_unary()
        return parse_primary()

    return parse_expr()


def evaluate(node: ASTNode, context: dict[str, Any]) -> float:
    if node.type == "number":
        return node.value
    elif node.type == "ident":
        return context.get(node.value, 0.0)
    elif node.type == "binary_op":
        left = evaluate(node.children[0], context)
        right = evaluate(node.children[1], context)
        if node.value == "+":
            return left + right
        elif node.value == "-":
            return left - right
        elif node.value == "*":
            return left * right
        elif node.value == "/":
            return left / right if right != 0 else 0.0
        elif node.value == ">":
            return 1.0 if left > right else 0.0
        elif node.value == "<":
            return 1.0 if left < right else 0.0
        elif node.value == ">=":
            return 1.0 if left >= right else 0.0
        elif node.value == "<=":
            return 1.0 if left <= right else 0.0
        elif node.value == "==":
            return 1.0 if left == right else 0.0
        elif node.value == "!=":
            return 1.0 if left != right else 0.0
    elif node.type == "unary_op":
        val = evaluate(node.children[0], context)
        return -val if node.value == "-" else val
    elif node.type == "ternary":
        cond = evaluate(node.children[0], context)
        if cond != 0:
            return evaluate(node.children[1], context)
        else:
            return evaluate(node.children[2], context)
    elif node.type == "call":
        args = [evaluate(a, context) for a in node.children]
        func_name = node.value
        import builtins

        from strategy_layer.operator_lib import cs_operators, helper_ops, ts_operators

        builtin = getattr(builtins, func_name, None)
        if callable(builtin):
            return float(builtin(*args))
        for mod in [ts_operators, cs_operators, helper_ops]:
            func = getattr(mod, func_name, None)
            if func:
                return func(*args)
        raise ValueError(f"Unknown function: {func_name}")

    return 0.0

# ── 序列模式求值 (alpha 表达式用) ──────────────────────────────────────────
# 每个节点返回 list[float] (每 bar 一个值)。时序算子做滚动窗口，
# 嵌套表达式 (如 delay(ts_sum(open,5),10)) 因此可以正确传播序列。

_ROLLING1 = {"ts_sum", "ts_rank", "ts_stddev", "ts_zscore", "ts_min", "ts_max",
             "ts_argmax", "ts_argmin", "ts_skew", "ts_kurt",
             "ts_decaylinear", "ts_wma", "stddev"}
_ROLLING2 = {"ts_corr", "ts_covariance", "correlation", "covariance"}
_SHIFT = {"ts_delay", "ts_delta", "delay", "delta"}
_PREFIX = {"rank", "scale", "demean"}  # 对前缀序列取末位排位的算子

# 刀4案2⑦: numpy 向量化实现 (vec_ops.py)。每类分支先查注册表走向量化,
# 返回 None = 向量化放弃 (如 ts_corr window<=0 的病态语义) → 回退原逐 bar
# 标量循环, 零行为变更。对拍 363/363 PASS (docs/alpha-vec-tolerance.md)。
from strategy_layer.expression import vec_ops as _vec


def _resolve_func(func_name: str):
    from strategy_layer.operator_lib import cs_operators, helper_ops, ts_operators
    for mod in (ts_operators, cs_operators, helper_ops):
        func = getattr(mod, func_name, None)
        if func:
            return func
    import builtins
    builtin = getattr(builtins, func_name, None)
    return builtin if callable(builtin) else None


def _align(series_list: list[list[float]], n: int) -> list[list[float]]:
    """右对齐补齐到长度 n (左侧补 0.0)"""
    out = []
    for s in series_list:
        if len(s) < n:
            s = [0.0] * (n - len(s)) + s
        out.append(s[-n:] if len(s) > n else s)
    return out


def evaluate_series(node: ASTNode, context: dict[str, Any]) -> list[float]:
    """序列模式求值: 返回每个 bar 的因子值序列 (长度 = context 中最长序列)"""
    n = 1
    for v in context.values():
        if isinstance(v, (list, tuple)):
            n = max(n, len(v))

    if node.type == "number":
        return [float(node.value)] * n
    if node.type == "ident":
        ts = context.get(node.value + "_ts")
        if isinstance(ts, (list, tuple)):
            return [float(x) for x in ts]
        return [float(context.get(node.value, 0.0))] * n
    if node.type == "unary_op":
        vals = evaluate_series(node.children[0], context)
        return [-v if node.value == "-" else v for v in vals]
    if node.type == "binary_op":
        left = evaluate_series(node.children[0], context)
        right = evaluate_series(node.children[1], context)
        left, right = _align([left, right], n)
        op = node.value
        out = []
        for a, b in zip(left, right):
            if op == "+": out.append(a + b)
            elif op == "-": out.append(a - b)
            elif op == "*": out.append(a * b)
            elif op == "/": out.append(a / b if b != 0 else 0.0)
            elif op == ">": out.append(1.0 if a > b else 0.0)
            elif op == "<": out.append(1.0 if a < b else 0.0)
            elif op == ">=": out.append(1.0 if a >= b else 0.0)
            elif op == "<=": out.append(1.0 if a <= b else 0.0)
            elif op == "==": out.append(1.0 if a == b else 0.0)
            elif op == "!=": out.append(1.0 if a != b else 0.0)
            else: out.append(0.0)
        return out
    if node.type == "ternary":
        cond = evaluate_series(node.children[0], context)
        t_branch = evaluate_series(node.children[1], context)
        f_branch = evaluate_series(node.children[2], context)
        cond, t_branch, f_branch = _align([cond, t_branch, f_branch], n)
        return [t if c != 0 else f for c, t, f in zip(cond, t_branch, f_branch)]
    if node.type == "call":
        func_name = node.value
        arg_series = [evaluate_series(ch, context) for ch in node.children]
        arg_series = _align(arg_series, n) if arg_series else []
        func = _resolve_func(func_name)
        if func is None:
            raise ValueError(f"Unknown function: {func_name}")

        if func_name in _ROLLING1 and arg_series:
            window = int(arg_series[1][-1]) if len(arg_series) > 1 else 5
            values = arg_series[0]
            vfn = _vec.ROLLING1.get(func_name)
            if vfn is not None:
                r = vfn(values, window)
                if r is not None:
                    return r
            return [float(func(values[: i + 1], window)) for i in range(n)]
        if func_name in _ROLLING2 and len(arg_series) >= 2:
            window = int(arg_series[2][-1]) if len(arg_series) > 2 else 5
            a, b = arg_series[0], arg_series[1]
            vfn = _vec.ROLLING2.get(func_name)
            if vfn is not None:
                r = vfn(a, b, window)
                if r is not None:
                    return r
            return [float(func(a[: i + 1], b[: i + 1], window)) for i in range(n)]
        if func_name in _SHIFT and arg_series:
            k = int(arg_series[1][-1]) if len(arg_series) > 1 else 1
            values = arg_series[0]
            vfn = _vec.SHIFT.get(func_name)
            if vfn is not None:
                r = vfn(values, k)
                if r is not None:
                    return r
            return [float(func(values[: i + 1], k)) for i in range(n)]
        if func_name in _PREFIX and arg_series:
            values = arg_series[0]
            vfn = _vec.PREFIX.get(func_name)
            if vfn is not None:
                r = vfn(values)
                if r is not None:
                    return r
            return [float(func(values[: i + 1])) for i in range(n)]
        if func_name == "adv20" and arg_series:
            values = arg_series[0]
            r = _vec.PREFIX["adv20"](values)
            if r is not None:
                return r
            return [float(func(values[: i + 1])) for i in range(n)]
        # 逐 bar 标量算子 (sign/log/abs/signedpower/clip/...)
        out = []
        for i in range(n):
            args_i = [s[i] for s in arg_series]
            try:
                r = func(*args_i)
                if isinstance(r, (list, tuple)):
                    r = r[-1] if r else 0.0
                out.append(float(r))
            except Exception:
                out.append(0.0)
        return out
    return [0.0] * n
