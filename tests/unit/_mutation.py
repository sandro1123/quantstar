"""变异验证手法 (B席 09-16, 统筹 sm-069d97397b2244d4 裁定归 B 席)。

## 病

一个**从不报红的断言**与**没有断言**等价。今夜反复出现的三例同族病：
- `cron_watchdog` 假绿 (监控永绿, 真挂了也不响)
- health 永久 red (锚错列, 每天 red 但结构上不可能消解)
- `TestMigrationV3` 写死 `version==6` (测的是"没变", 不是"对了")

## 药

**顺序/时序断言必须附一条「改回错误顺序会红」的验证。**
推广: **任何断言都该能回答「什么情况下我会红」** —— 若答不出, 它是装饰。

本模块提供两个工具:

1. `assert_order(calls, expected)` —— 顺序断言 (清晰失败信息)
2. `assert_mutation_caught(setup, mutate, check, expect_msg)` —— **变异验证**:
   在 `mutate` 注入错误后, 断言 `check` **确实报错**。若变异后 check 仍绿
   → 说明 check 不敏感 (永绿), 本工具主动 fail 并给出诊断。

## 用法

```python
from tests.unit._mutation import assert_order, assert_mutation_caught

# 1) 顺序断言
assert_order(trace.calls, ["snapshot", "benchmark"])

# 2) 变异验证: 把顺序改回错的, 断言顺序断言会红
def _wrong(): trace.calls = ["benchmark", "snapshot"]
assert_mutation_caught(
    mutate=_wrong,
    check=lambda: assert_order(trace.calls, ["snapshot", "benchmark"]),
    what="两腿顺序断言",
)
```

`assert_mutation_caught` 的语义: **变异后 check 必须抛 AssertionError**。
- 抛了 → 通过 (断言敏感, 抓得住真问题)
- 没抛 → `pytest.fail("断言不敏感(永绿): 注入 <what> 后仍通过")`
"""
from __future__ import annotations

import pytest


def assert_order(calls, expected, msg: str = ""):
    """断言调用顺序 (含清晰失败信息)。

    calls / expected 为序列; 用 == 比较 (长度与顺序都须一致)。
    失败时给出逐位对比, 便于定位。
    """
    got = list(calls)
    want = list(expected)
    if got == want:
        return
    detail = []
    for i in range(max(len(got), len(want))):
        g = got[i] if i < len(got) else "<缺>"
        w = want[i] if i < len(want) else "<多>"
        flag = "" if g == w else "  <-- 不符"
        detail.append(f"  [{i}] 实={g!r} 期={w!r}{flag}")
    head = msg or "调用顺序不符"
    raise AssertionError(
        f"{head}: 实={got} 期={want}\n" + "\n".join(detail))


def assert_mutation_caught(mutate, check, what: str = "该断言"):
    """变异验证: 注入错误后, check 必须报红 —— 否则断言不敏感 (永绿)。

    Args:
        mutate: 无参可调用, 就地注入"错误" (如把顺序改回错的)。
        check:  无参可调用, 执行被测断言; 期望它因 mutate 而抛 AssertionError。
        what:   诊断用名称。

    通过条件: mutate() 之后 check() **抛 AssertionError**。
    反例 (check 仍通过) → 直接 pytest.fail, 因为那意味着断言抓不住该错误。
    """
    mutate()
    try:
        check()
    except AssertionError as e:
        # 断言确实抓住了变异 —— 期望路径
        assert str(e) is not None  # 保底: 有失败信息
        return
    except Exception as e:  # noqa: BLE001
        # 非 AssertionError 的异常也算"报红", 但提示一下 (可能是误伤)
        raise AssertionError(
            f"变异验证: {what} 在注入错误后抛了非断言异常 "
            f"({type(e).__name__}: {e}) —— 算报红, 但请确认不是误伤"
        ) from e
    pytest.fail(
        f"断言不敏感 (永绿): 注入错误后 {what} 仍然通过。\n"
        f"一个从不报红的断言与没有断言等价 —— 请补强 {what}。"
    )


def assert_no_mutation_effect(mutate, check, what: str = "该断言"):
    """反向工具: 注入一个**无关**变更后, check 应仍通过 (防误伤/过刚)。

    与 assert_mutation_caught 配对: 一个证明"抓得住真错",
    一个证明"不冤枉无辜" —— 两向都过才是好断言。
    """
    mutate()
    try:
        check()
    except AssertionError as e:
        raise AssertionError(
            f"断言过刚 (误伤): 注入无关变更后 {what} 报红: {e}"
        ) from e
