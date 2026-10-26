"""MCP Registry — 工具注册 + 参数 schema 验证"""


class SchemaValidationError(Exception):
    pass


class MCPRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(self, name: str, description: str, input_schema: dict, handler: callable):
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }

    def get_tool(self, name: str) -> dict | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        return [{"name": n, "description": t["description"], "input_schema": t["input_schema"]}
                for n, t in self._tools.items()]

    def _validate_params(self, params: dict, schema: dict) -> list[str]:
        errors = []
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for field in required:
            if field not in params:
                errors.append(f"缺少必填参数: {field}")
        for key, value in params.items():
            prop = properties.get(key)
            if prop:
                expected_type = prop.get("type", "string")
                if expected_type == "string" and not isinstance(value, str):
                    errors.append(f"{key} 应为 string, 实际 {type(value).__name__}")
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    errors.append(f"{key} 应为 number, 实际 {type(value).__name__}")
                elif expected_type == "integer" and not isinstance(value, int):
                    errors.append(f"{key} 应为 integer, 实际 {type(value).__name__}")
        return errors

    def call(self, name: str, params: dict) -> dict:
        tool = self.get_tool(name)
        if tool is None:
            return {"error": f"tool '{name}' not found"}
        errors = self._validate_params(params, tool.get("input_schema", {}))
        if errors:
            return {"error": "; ".join(errors)}
        return tool["handler"](params)
