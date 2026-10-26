"""MCP Bridge — 真实 MCP 协议通信 (stdio/SSE)"""
import asyncio
import json
from typing import Any

from common.logging import get_logger

logger = get_logger("agent.mcp")


class MCPBridge:
    def __init__(self):
        self._servers: dict[str, dict] = {}
        self._processes: dict[str, Any] = {}

    def register_server(self, name: str, transport: str, command: list[str] | None = None,
                         url: str | None = None):
        self._servers[name] = {
            "transport": transport,
            "command": command or [],
            "url": url or "",
        }

    async def call(self, server: str, tool: str, params: dict | None = None) -> Any:
        config = self._servers.get(server)
        if not config:
            logger.warning(f"MCP server '{server}' not registered")
            return None

        request = {"tool": tool, "params": params or {}}

        if config["transport"] == "stdio":
            return await self._call_stdio(config, request)
        elif config["transport"] == "sse":
            return await self._call_sse(config, request)
        else:
            logger.warning(f"Unknown transport: {config['transport']}")
            return None

    async def _call_stdio(self, config: dict, request: dict) -> Any:
        try:
            proc = await asyncio.create_subprocess_exec(
                *config["command"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(request).encode()),
                timeout=30.0,
            )
            if stderr:
                logger.warning(f"MCP stdio stderr: {stderr.decode()[:200]}")
            if stdout:
                return json.loads(stdout.decode())
            return None
        except TimeoutError:
            logger.warning(f"MCP stdio timeout: {config['command']}")
            return None
        except Exception as e:
            logger.warning(f"MCP stdio error: {e}")
            return None

    async def _call_sse(self, config: dict, request: dict) -> Any:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(config["url"], json=request, timeout=30) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"MCP SSE status {resp.status}")
                    return None
        except Exception as e:
            logger.warning(f"MCP SSE error: {e}")
            return None

    def health(self) -> dict:
        return {
            "servers_registered": len(self._servers),
            "servers": list(self._servers.keys()),
        }
