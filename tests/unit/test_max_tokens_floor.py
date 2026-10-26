"""TDD: max_tokens 预算下限守卫 (2026-10-22 晨报 empty response 止血).

病灶: hy3 是 reasoning 模型, 思考过程吃 reasoning_content, 正文 content 可能为空。
max_tokens 给小 (150/200/300/400/600) => finish_reason=length + content 空 =>
客户端判 "empty response" => instructor 短路 => 走规则腿 => 晨报正文空 (BODY_LEN=0)。

钉1 (全仓下限): src/ + scripts/ 下任何 max_tokens 显式赋值不得 < 600。
     防止后人把数字调回去 (本题的病根就是"数字被写死在调用点, 无人守")。
     ⚠️ 扫描根必须含 scripts/ —— scripts/ 下的晨报/午报/晚报脚本是真实投递路径,
     扫不到 = 永久盲区 (统筹裁示 2026-10-22)。
钉2 (自证真防护): 扫描器本身必须能扫到非空集合, 且必须覆盖 src/ 与 scripts/ 两处,
     否则其中之一是永久盲区, 且钉1 可能是"扫描器坏了导致恒绿"的假绿。
     (我第一版 SRC_ROOT 少一层 → 扫到 0 个 → 钉1 假绿通过, 靠钉2 抓出)
钉3 (扫描器自身完备性): 扫描器必须能抓到**函数定义默认值**形态的 max_tokens
     (scripts/ 下 _llm_call(prompt, timeout_s=20, max_tokens=400, point=...) 就是这种),
     不能只抓调用点 —— 抓不到 = 晨报/午报/晚报脚本的默认预算永远无人守。
"""

import ast
import pathlib
from typing import Iterator

MIN_MAX_TOKENS = 600
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# 扫描根: src/ (生产代码) + scripts/ (真实投递脚本) —— 两者都调 LLM, 都受 max_tokens 截断之害。
SCAN_ROOTS = ("src", "scripts")


def _function_default_max_tokens(node) -> Iterator[tuple[int, int]]:
    """产出该函数定义里 max_tokens 参数的默认值 (行号, 值)。

    覆盖两种形态:
      ① 位置参数默认值:  def f(a, max_tokens=400, b=1)
      ② 关键字专用参数:  def f(*, max_tokens=400)
    两者都必须抓 —— 晨报脚本的 _llm_call 是 ①, 只抓 ② 会漏。
    """
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    if defaults:
        # defaults 对应 positional 的尾部 (最后 len(defaults) 个)
        tail = positional[len(positional) - len(defaults):]
        for arg_node, default in zip(tail, defaults):
            if arg_node.arg == "max_tokens" and isinstance(default, ast.Constant) \
                    and isinstance(default.value, int):
                yield (default.lineno, default.value)
    for arg_node, default in zip(args.kwonlyargs, args.kw_defaults):
        if arg_node.arg == "max_tokens" and default is not None \
                and isinstance(default, ast.Constant) and isinstance(default.value, int):
            yield (default.lineno, default.value)


def _iter_max_tokens_assignments() -> Iterator[tuple[str, int, int]]:
    """产出 (文件相对路径, 行号, 值) — AST 扫描所有 max_tokens 显式数值。"""
    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = path.relative_to(REPO_ROOT)
            for node in ast.walk(tree):
                # ① 调用点关键字参数 max_tokens=NNN
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "max_tokens" and isinstance(kw.value, ast.Constant) \
                                and isinstance(kw.value.value, int):
                            yield (str(rel), kw.value.lineno, kw.value.value)
                # ② 函数定义默认值 max_tokens=NNN
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for lineno, value in _function_default_max_tokens(node):
                        yield (str(rel), lineno, value)


def test_no_max_tokens_below_floor():
    """钉1: src/ + scripts/ 下不得存在 max_tokens < 600 的显式数值。"""
    offenders = [
        (f, ln, v) for f, ln, v in _iter_max_tokens_assignments()
        if v < MIN_MAX_TOKENS
    ]
    assert not offenders, (
        f"max_tokens 低于下限 {MIN_MAX_TOKENS} 的调用点 (reasoning 模型正文会被截断为空): "
        f"{offenders}"
    )


def test_guard_scans_both_src_and_scripts():
    """钉2 (自证真防护 + 覆盖范围钉): 扫描器必须扫到 src/ 与 scripts/ 两处,
    否则其中之一是永久盲区 (scripts/ 是真实投递路径, 晨报正文空就出在那)。"""
    sample = list(_iter_max_tokens_assignments())
    assert sample, "扫描器必须至少扫到一处 max_tokens 赋值 (否则钉1 是假绿)"
    roots_hit = {p.split("/")[0] for p, _, _ in sample}
    assert "src" in roots_hit, f"扫描器未覆盖 src/ (实得 {roots_hit})"
    assert "scripts" in roots_hit, f"扫描器未覆盖 scripts/ (实得 {roots_hit})"
    assert all(isinstance(v, int) for _, _, v in sample)


def test_guard_catches_function_default_form():
    """钉3 (扫描器完备性): 扫描器必须能抓到**函数定义默认值**形态。

    scripts/ 的 _llm_call(prompt, timeout_s=20, max_tokens=400, point=...) 就是此形态 ——
    只抓调用点的话, 脚本默认预算永远无人守, 晨报路径仍是盲区。
    """
    sample = list(_iter_max_tokens_assignments())
    script_files = [p for p, _, _ in sample if p.startswith("scripts/")]
    assert script_files, "scripts/ 下必须扫到 max_tokens 赋值"
    # 反向验证: 把某个 scripts 里的函数默认值改小, 扫描器必须报出来
    # (用真实文件做对照 —— 扫到 scripts 里至少有一处「函数定义默认值」形态)
    default_form_found = False
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if list(_function_default_max_tokens(node)):
                    default_form_found = True
                    break
        if default_form_found:
            break
    assert default_form_found, (
        "扫描器必须能识别函数定义默认值的 max_tokens 形态 "
        "(晨报/午报/晚报脚本的 _llm_call 就是此形态)")


def test_documented_small_budget_are_intentional():
    """钉4 (阈值语义钉): 区分「下限 600」与「目标 2000」两个概念。

    背景 (2026-10-22 哨兵复查): 全仓还有 3 处 max_tokens=800 ——
      - src/agent_layer/agents/decision_agent.py:38/57 (决策腿)
      - src/agent_layer/llm/structured_output.py:72 (instructor 结构化输出)
    它们**不是漏网**, 而是 800 >= 600 通过了钉1 的下限判定。

    下限 600 = 任何腿都不得低于此值 (低于必然 reasoning 截断为空)。
    目标 2000 = 长篇叙述腿 (晨报/晚报/午报 section) 的余量值。

    决策/结构化腿输出的是短 JSON ({verdict, confidence, reasoning}),
    800 是有意为之 —— 本钉把这条意图写死, 防下一轮"统一下限"把它当漏网再翻一次。
    """
    sample = list(_iter_max_tokens_assignments())
    small = [(p, ln, v) for p, ln, v in sample if v < 2000]
    allowed_files = {
        "src/agent_layer/agents/decision_agent.py",
        "src/agent_layer/llm/structured_output.py",
    }
    for path, lineno, value in small:
        assert value >= MIN_MAX_TOKENS, (
            f"{path}:{lineno} max_tokens={value} 低于下限 {MIN_MAX_TOKENS}")
        assert path in allowed_files, (
            f"{path}:{lineno} max_tokens={value} < 2000 但不在豁免清单里 —— "
            "长篇叙述腿必须用 2000, 若这是新的短结构化腿, 请加入 allowed_files 并写清理由")
