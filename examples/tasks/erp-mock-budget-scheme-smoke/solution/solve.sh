#!/bin/bash
# oracle：经 MCP 代理调用预算方案查询工具并写产物（需要 trial 网络里有 lg-mcp-proxy + erp-mock）。
set -euo pipefail
python3 - "${BENCH_WORKSPACE:-/workspace}" <<'PY'
import json, sys, urllib.request
from pathlib import Path
PROXY = "http://lg-mcp-proxy:18090/mcp"
TOOL = "budget_scheme_query"
TOP_N = 5
def rpc(body, sid=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(PROXY, data=json.dumps(body).encode(), method="POST", headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    raw = resp.read().decode("utf-8", "replace")
    msg = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            msg = json.loads(line[5:].strip()); break
    if msg is None and raw.strip():
        msg = json.loads(raw)
    return resp, msg
resp, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05",
     "capabilities": {}, "clientInfo": {"name": "oracle", "version": "0"}}})
sid = resp.headers.get("mcp-session-id")
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
# 声明式查询的参数都可省；全省时 history 返回该实体的全部行，按记录 ID 升序。
_, res = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": TOOL,
           "arguments": {}}}, sid)
rows = res["result"]["structuredContent"]["history"][:TOP_N]
schemes = [{"number": r["src_number"], "name": r["src_name"]} for r in rows]
out = Path(sys.argv[1]) / "output/budget-schemes.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"tool": TOOL, "schemes": schemes}, ensure_ascii=False, indent=2))
PY
