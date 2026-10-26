# -*- coding: utf-8 -*-
"""provider_manager.py 记账接线补丁（幂等）。

把 `record_usage(call_point, max(1, len(str(result)) // 3))`
改为：经 usage_sink 取真实 token，拿不到才回落估算。
"""
import io

P = "src/agent_layer/llm/provider_manager.py"
src = io.open(P, encoding="utf-8", newline="").read()
orig = src

# 1. call_llm 闭包里带 usage_sink
src = src.replace(
    """        # Middleware 执行
        def call_llm(p: str) -> str | None:
            result = self.client.chat(
                prompt=p, system_prompt=adapted_sys,
                temperature=temperature, max_tokens=max_tokens,
                call_point=call_point, timeout=timeout,
            )""",
    """        # Middleware 执行
        # 2026-09-24 辩论腿止血配套: usage_sink 回传真实 token;
        # 拿不到时回落字符估算 (口径标记见 TokenBudget.usage_source)。
        _usage_sink: dict = {}
        def call_llm(p: str) -> str | None:
            result = self.client.chat(
                prompt=p, system_prompt=adapted_sys,
                temperature=temperature, max_tokens=max_tokens,
                call_point=call_point, timeout=timeout,
                usage_sink=_usage_sink,
            )""",
    1)

# 2. 记账改真实优先
src = src.replace(
    """            self.token_budget.record_usage(call_point, max(1, len(str(result)) // 3))""",
    """            # 真实 token 优先 (provider 回传 usage); 缺失才用字符估算兜底。
            self.token_budget.record_usage(
                call_point, text=str(result),
                real_tokens=_usage_sink.get("total_tokens"))""",
    1)

assert src != orig, "补丁未命中（脚本失效，勿上传）"
io.open(P, "w", encoding="utf-8", newline="").write(src)
print("patched OK; CRLF=", src.count("\r"))
