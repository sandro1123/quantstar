"""PromptAdapter — 模型特定 prompt 适配"""


class PromptAdapter:
    def adapt(self, prompt: str, model_name: str, system_prompt: str | None = None) -> tuple[str, str | None]:
        model_lower = model_name.lower() if model_name else ""

        if "qwen" in model_lower:
            adapted = f"请用中文回答。\n{prompt}"
            adapted_sys = system_prompt
        elif "deepseek" in model_lower:
            adapted = f"请严格按照JSON格式输出。\n{prompt}"
            adapted_sys = (system_prompt or "") + "\n输出格式: JSON"
        elif "llama" in model_lower:
            adapted = f"Please answer in English.\n{prompt}"
            adapted_sys = system_prompt
        elif "yi" in model_lower or "glm" in model_lower or "chatglm" in model_lower:
            adapted = f"请用中文回答。\n{prompt}"
            adapted_sys = system_prompt
        else:
            adapted = prompt
            adapted_sys = system_prompt

        return adapted, adapted_sys
