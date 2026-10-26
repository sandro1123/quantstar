"""P-LLM-1 验证: 加载 llm.yaml 的 providers, 真实调用 chat() 确认出词。
期望: sensenova key1 可用(200 出词), 失败时 fallback 到 nvidia。
只读, 不触发 OpenRouter 发现(enable_discovery=false)。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("CONFIG_ENV", "prod")

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agent_layer.llm.client import LLMClient


def main():
    client = LLMClient()
    print("providers(按priority排序):")
    for p in client._providers:
        key = os.environ.get(p.get("api_key_env", ""), "")
        print(f"  {p['name']:22s} priority={p['priority']} model={p['model']} has_key={bool(key)}")

    print("\nenable_discovery =", getattr(client, "_enable_discovery", "未设置"))

    print("\n--- 真实 chat() 调用(出词验证) ---")
    text = client.chat(
        prompt="请只回复两个字: 成功",
        system_prompt="你是验证助手。",
        call_point="p-llm1-verify",
        timeout=25,
    )
    print("chat() 返回值:", repr(text))
    if text:
        print("=> LLM 链路可用 ✅")
    else:
        print("=> LLM 全部失败 ❌")


if __name__ == "__main__":
    main()