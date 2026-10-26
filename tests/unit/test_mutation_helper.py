"""变异验证手法自测 (B席 09-16)。

手法本身也须被验证: 用变异法测变异法 —— 证明
  assert_mutation_caught 对「敏感断言」放行, 对「永绿断言」报警。
"""
import pytest

from tests.unit._mutation import (
    assert_mutation_caught,
    assert_no_mutation_effect,
    assert_order,
)


class _Trace:
    def __init__(self):
        self.calls = ["snapshot", "benchmark"]


# ── assert_order 本体 ──────────────────────────────────────────────

def test_assert_order_passes_on_match():
    assert_order(["a", "b"], ["a", "b"])


def test_assert_order_red_on_wrong_order():
    with pytest.raises(AssertionError):
        assert_order(["b", "a"], ["a", "b"])


def test_assert_order_red_on_missing_element():
    with pytest.raises(AssertionError):
        assert_order(["a"], ["a", "b"])


def test_assert_order_message_locates_mismatch():
    try:
        assert_order(["a", "c"], ["a", "b"])
    except AssertionError as e:
        assert "[1]" in str(e) and "不符" in str(e)
    else:
        pytest.fail("应报红")


# ── assert_mutation_caught: 核心语义 ───────────────────────────────

def test_mutation_caught_when_assertion_is_sensitive():
    """敏感断言: 变异后报红 → 工具放行 (这正是我们要的)。"""
    t = _Trace()
    assert_mutation_caught(
        mutate=lambda: setattr(t, "calls", ["benchmark", "snapshot"]),
        check=lambda: assert_order(t.calls, ["snapshot", "benchmark"]),
        what="顺序断言",
    )


def test_mutation_caught_flags_always_green_assertion():
    """永绿断言: 变异后仍通过 → 工具必须 pytest.fail (核心价值)。"""
    t = _Trace()
    with pytest.raises(BaseException) as ei:
        assert_mutation_caught(
            mutate=lambda: setattr(t, "calls", ["benchmark", "snapshot"]),
            # 永绿: 只断言"有元素", 不检查顺序
            check=lambda: (len(t.calls) == 2) or pytest.fail("x"),
            what="空泛断言",
        )
    assert "不敏感" in str(ei.value) or "永绿" in str(ei.value)


def test_mutation_caught_accepts_non_assertion_exception():
    """变异后抛非 AssertionError 也算报红, 但提示确认 (不静默)。

    实现语义: 非断言异常会被包装成 AssertionError 抛出 (带"请确认不是误伤"),
    故此处期望 AssertionError 且信息含"误伤"提示。
    """
    def _boom():
        raise ValueError("boom")
    with pytest.raises(AssertionError) as ei:
        assert_mutation_caught(
            mutate=lambda: None,
            check=_boom,
            what="抛错断言",
        )
    assert "误伤" in str(ei.value)


def test_mutation_caught_red_when_check_raises_assertion_on_clean():
    """反向: check 在**未变异**时就红 → 说明 check 本身坏了 (会由 mutate 前的
    调用暴露)。此测验证 mutate 一定会被调用 (副作用可观测)。"""
    called = []
    assert_mutation_caught(
        mutate=lambda: called.append(1),
        check=lambda: (_ for _ in ()).throw(AssertionError("red")),
        what="x",
    )
    assert called == [1], "mutate 必须先于 check 执行"


# ── assert_no_mutation_effect: 防误伤 ──────────────────────────────

def test_no_mutation_effect_passes_for_irrelevant_change():
    t = _Trace()
    assert_no_mutation_effect(
        mutate=lambda: setattr(t, "extra", "irrelevant"),
        check=lambda: assert_order(t.calls, ["snapshot", "benchmark"]),
        what="顺序断言",
    )


def test_no_mutation_effect_red_when_overstrict():
    t = _Trace()
    with pytest.raises(AssertionError) as ei:
        assert_no_mutation_effect(
            mutate=lambda: setattr(t, "calls", ["x", "y"]),  # 真改了相关面
            check=lambda: assert_order(t.calls, ["snapshot", "benchmark"]),
            what="顺序断言",
        )
    assert "过刚" in str(ei.value)


# ── 纪律文档载体验证 (防"写了但会漂") ──────────────────────────────

def test_discipline_is_documented_in_agents_md():
    """纪律必须有载体: AGENTS.md 须写明"断言必须附变异验证"。

    防的是: 工具在, 但纪律只靠人记 —— 新人/新会话不知道要用。
    变异法: 若从 AGENTS.md 删掉该段, 本测报红。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    agents = root / "AGENTS.md"
    assert agents.exists(), "AGENTS.md 须存在"
    text = agents.read_text(encoding="utf-8")
    assert "_mutation.py" in text, "纪律须指向工具模块"
    assert "变异验证" in text, "纪律须具名"
    assert "assert_mutation_caught" in text, "纪律须给出工具名 (可执行载体)"
