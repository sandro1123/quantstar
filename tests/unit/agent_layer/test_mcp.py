"""MCP 层测试 — Bridge / Registry"""
import pytest
from agent_layer.mcp.bridge import MCPBridge
from agent_layer.mcp.registry import MCPRegistry, SchemaValidationError


def test_mcp_bridge_register():
    bridge = MCPBridge()
    bridge.register_server("test", "stdio", command=["echo", "{}"])
    health = bridge.health()
    assert health["servers_registered"] == 1
    assert "test" in health["servers"]


def test_mcp_bridge_unknown_server():
    bridge = MCPBridge()
    import asyncio
    result = asyncio.run(bridge.call("unknown", "tool"))
    assert result is None


def test_mcp_registry_register():
    reg = MCPRegistry()
    reg.register("echo", "回声测试", {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
                 handler=lambda p: {"echo": p.get("msg")})
    tools = reg.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "echo"


def test_mcp_registry_call():
    reg = MCPRegistry()
    reg.register("echo", "回声测试", {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
                 handler=lambda p: {"echo": p.get("msg")})
    result = reg.call("echo", {"msg": "hello"})
    assert result["echo"] == "hello"


def test_mcp_registry_unknown_tool():
    reg = MCPRegistry()
    result = reg.call("unknown", {})
    assert "error" in result


def test_mcp_registry_missing_required():
    reg = MCPRegistry()
    reg.register("echo", "回声测试", {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
                 handler=lambda p: {"echo": p.get("msg")})
    result = reg.call("echo", {})
    assert "error" in result
    assert "缺少必填参数" in result["error"]


def test_mcp_registry_type_check():
    reg = MCPRegistry()
    reg.register("add", "加法", {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                                 "required": ["a", "b"]},
                 handler=lambda p: {"sum": p["a"] + p["b"]})
    result = reg.call("add", {"a": "not_a_number", "b": 2})
    assert "error" in result