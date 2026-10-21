"""ERP mock 连通性冒烟的 verifier：产物结构 + 由代理真实取数复核。

复核方式：verifier 在 trial 网络里直接经 MCP 代理（http://lg-mcp-proxy:18090/mcp）
调用同一个预算方案查询工具，拿真实返回与 agent 产物逐条比对，不依赖 agent 自报。
mock 的 instances/erp-benchmark 是确定性数据：不同会话（不同沙箱）相同初始数据集返回逐条一致，
所以 verifier 的独立会话能复核 agent 会话的结果。
代理不可达时（例如本地 oracle 自测）只校验结构，reward 记 0 并注明原因。

本题的数据集是 bgt-05（实例的 default_dataset），
预算方案共 16 条，这里只比对前 5 条。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

TOOL = "budget_scheme_query"
PROXY = "http://lg-mcp-proxy:18090/mcp"
ARGS: dict = {}
TOP_N = 5
# 产物字段 → 返回字段。上游名为 number/name 的字段在 mock 侧加了 src_ 前缀，
# 因为加载第 8 步会把实体记录的 name/number 拿去和工具描述做子串比对（需求 3 第 5 条）。
FIELD_MAP = {"number": "src_number", "name": "src_name"}
FIELDS = tuple(FIELD_MAP)
DELIVERABLE = "output/budget-schemes.json"
ITEMS_KEY = "schemes"


def rpc(body: dict, sid: str | None = None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(PROXY, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    raw = resp.read().decode("utf-8", "replace")
    msg = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            msg = json.loads(line[5:].strip())
            break
    # 通知（notifications/initialized）返回空体，无需解析；只有有内容时才按 JSON 解析。
    if msg is None and raw.strip():
        msg = json.loads(raw)
    return resp, msg


def live_items() -> list[dict]:
    # 除 initialize 外都要带 Mcp-Session-Id（mock 的协议要求），会话 id 由 initialize 响应头给出。
    resp, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "lg-verifier", "version": "0"}}})
    sid = resp.headers.get("mcp-session-id")
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, res = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": TOOL, "arguments": ARGS}}, sid)
    rows = res["result"]["structuredContent"]["history"][:TOP_N]
    return [{out: r.get(src) for out, src in FIELD_MAP.items()} for r in rows]


def main(workspace: str, log_dir: str) -> int:
    logs = Path(log_dir)
    logs.mkdir(parents=True, exist_ok=True)
    checks: dict[str, bool] = {}
    notes: list[str] = []
    out = Path(workspace) / DELIVERABLE
    result = None
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
        checks["deliverable"] = True
    except (OSError, json.JSONDecodeError) as exc:
        checks["deliverable"] = False
        notes.append(f"output unreadable: {type(exc).__name__}")
    reported_items = result.get(ITEMS_KEY) if isinstance(result, dict) else None
    checks["schema"] = (isinstance(reported_items, list) and len(reported_items) > 0 and all(
        isinstance(s, dict) and set(FIELDS) <= s.keys() for s in reported_items))
    # 各 harness 给 MCP 工具加的前缀不同（OpenCode erp_…，CC/Pi/DSH mcp__erp__…）；
    # 只要求报告的名字以原名结尾，真实性由下面的实时复核保证。
    reported = result.get("tool") if isinstance(result, dict) else None
    checks["tool_named"] = isinstance(reported, str) and reported.endswith(TOOL)
    if isinstance(reported, str) and reported != TOOL:
        notes.append(f"reported tool name: {reported}")
    try:
        expected = live_items()
        checks["matches_live_erp"] = checks["schema"] and [
            {k: s.get(k) for k in FIELDS} for s in reported_items
        ] == expected
        notes.append(f"live rows: {len(expected)}")
    except Exception as exc:
        checks["matches_live_erp"] = False
        notes.append(f"live check failed: {type(exc).__name__}: {exc}")
    reward = 1 if all(checks.values()) else 0
    (logs / "reward.txt").write_text(str(reward))
    (logs / "details.json").write_text(json.dumps({"checks": checks, "notes": notes},
                                                  ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
