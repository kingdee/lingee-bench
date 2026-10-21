"""把 Harbor 的 ``mcp_servers``（任务 ``[[environment.mcp_servers]]`` / job ``agents[].mcp_servers``）
翻译成各 harness 的原生 MCP 配置。

约定：评测任务里的 MCP 一律指向每个 trial 的 MCP 代理 sidecar
（``http://lg-mcp-proxy:18090/mcp``，见 ``configs/compose/mcp-proxy.yaml``），
由代理决定转发到真实 ERP 还是 mock，并注入密钥。这里生成的配置只含代理地址，不含任何密钥。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

MCP_PROXY_COMPOSE = "configs/compose/mcp-proxy.yaml"
MCP_PROXY_HOST = "lg-mcp-proxy"


def _check_name(name: str, harness: str) -> None:
    # Pi / DeepSeek Harness 只接受 [A-Za-z0-9_-]；统一按最严格的规则校验，保证各 harness 工具名一致。
    if not name or len(name) > 32 or any(not (c.isalnum() or c in "-_") for c in name):
        raise ValueError(f"{harness}: MCP server name {name!r} must match [A-Za-z0-9_-]{{1,32}}")


def mcp_servers_json(servers: Iterable[Any], *, harness: str, http_type: str = "http",
                     allow_sse: bool = True, extra: dict[str, Any] | None = None) -> dict:
    """生成 Claude Code 风格的 ``{"mcpServers": {...}}``（Pi 同源格式）。

    ``http_type``：streamable-http 在目标 harness 里的 type 写法；``extra`` 合并进每个条目。
    """
    out: dict[str, dict[str, Any]] = {}
    for server in servers:
        _check_name(server.name, harness)
        if server.transport == "stdio":
            entry: dict[str, Any] = {"type": "stdio", "command": server.command,
                                     "args": list(server.args)}
        elif server.transport == "sse":
            if not allow_sse:
                raise ValueError(f"{harness}: MCP server {server.name!r}: sse transport is not "
                                 "supported; use streamable-http")
            entry = {"type": "sse", "url": server.url}
        else:
            entry = {"type": http_type, "url": server.url}
        if extra:
            entry.update(extra)
        out[server.name] = entry
    return {"mcpServers": out}


def dumps(config: dict) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def uses_mcp_proxy(urls: Iterable[str | None]) -> bool:
    return any(url and f"//{MCP_PROXY_HOST}" in url for url in urls)
