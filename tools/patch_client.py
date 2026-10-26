# -*- coding: utf-8 -*-
"""client.py 的最小侵入补丁脚本（幂等）。

改三处：
  1. 新增 extract_usage_tokens(data) 工具函数
  2. chat() 签名加 usage_sink=None，透传给 _chat_impl
  3. _chat_impl() 签名加 usage_sink=None，成功返回前回填真实 token

默认 None ⇒ 既有调用行为零变化（判例：既有调用点不带实参 → 行为零变化）。
"""
import io
import re
import sys

P = "src/agent_layer/llm/client.py"
src = io.open(P, encoding="utf-8", newline="").read()
orig = src

# ---- 1. 工具函数（插在 class 定义前；用 chat 定义前的空行锚） ----
if "def extract_usage_tokens(" not in src:
    helper = '''

def extract_usage_tokens(data) -> int | None:
    """从 OpenAI 兼容响应里取真实 token 数；缺失/畸形返回 None（调用方回落估算）。

    2026-09-24 辩论腿止血配套：此前 provider_manager 记 len(str(result))//3
    把字符数当 token，与真实消耗不同源。改用真实 usage 后额度语义才准。
    """
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(pt, (int, float)) and isinstance(ct, (int, float)):
        return int(pt) + int(ct)
    return None

'''
    anchor = "class LLMClient"
    assert anchor in src, "找不到 class LLMClient 锚点"
    src = src.replace(anchor, helper.lstrip("\n") + "\n" + anchor, 1)

# ---- 2. chat() 签名 + 透传 ----
if "usage_sink" not in src.split("def _chat_impl")[0]:
    src = src.replace(
        """             cache_ttl: int = None, call_point: str = "unknown",
             timeout: int = None) -> str | None:""",
        """             cache_ttl: int = None, call_point: str = "unknown",
             timeout: int = None, usage_sink: dict | None = None) -> str | None:""",
        1)
    src = src.replace(
        """            future = executor.submit(
                self._chat_impl, prompt, system_prompt, temperature,
                max_tokens, cache_ttl, call_point, timeout,
            )""",
        """            future = executor.submit(
                self._chat_impl, prompt, system_prompt, temperature,
                max_tokens, cache_ttl, call_point, timeout, usage_sink,
            )""",
        1)

# ---- 3. _chat_impl 签名 + 回填 ----
src = src.replace(
    """    def _chat_impl(self, prompt, system_prompt, temperature, max_tokens,
                   cache_ttl, call_point, timeout):""",
    """    def _chat_impl(self, prompt, system_prompt, temperature, max_tokens,
                   cache_ttl, call_point, timeout, usage_sink=None):""",
    1)

src = src.replace(
    """                        if content and content.strip():
                            response_text = content.strip()
                            success = True""",
    """                        if content and content.strip():
                            response_text = content.strip()
                            success = True
                            # 2026-09-24: 回填真实 token（usage_sink 为 None 时零行为变化）
                            if usage_sink is not None:
                                _tok = extract_usage_tokens(data)
                                if _tok is not None:
                                    usage_sink["total_tokens"] = _tok""",
    1)

assert src != orig, "补丁未命中任何锚点（脚本失效，勿上传）"
io.open(P, "w", encoding="utf-8", newline="").write(src)
print("patched OK; CRLF=", src.count("\r"))
