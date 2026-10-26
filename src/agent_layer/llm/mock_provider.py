"""MockProvider — 测试用，确定性响应"""


class MockProvider:
    def __init__(self):
        self._responses: dict[str, str] = {}
        self._default = '{"verdict": "观望", "confidence": 0.5}'

    def register_response(self, code: str, response: str):
        self._responses[code] = response

    def chat(self, prompt: str, **kwargs) -> str | None:
        for code, resp in self._responses.items():
            if code in prompt:
                return resp
        return self._default

    def chat_structured(self, prompt: str, schema_class=None, **kwargs) -> dict | None:
        import json
        result = self.chat(prompt)
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return None
        return None
