"""每个 trial 的 MCP 代理 sidecar（只用标准库）。

harness 统一连这一层干净的 MCP 端点（无密钥），代理按 ``LG_MCP_TARGET`` 决定把请求
转发到真实 ERP MCP（注入密钥头）还是离线 mock MCP。密钥只存在于本容器环境变量，
harness 侧永远拿不到。

路由：
- ``LG_MCP_TARGET=real`` → 上游 ``LG_ERP_MCP_URL``，注入 ``LG_ERP_MCP_HEADERS``（JSON）
- ``LG_MCP_TARGET=mock`` → 上游 ``LG_MCP_MOCK_URL``，注入 ``LG_MCP_MOCK_HEADERS``（JSON，可选）

端点：
- ``POST/GET /mcp``  透明转发 MCP streamable-http（请求体是 JSON-RPC；响应可能是 JSON 或 SSE，原样回传）
- ``GET /health``    200

转发前确保 ``Accept: application/json, text/event-stream``（部分 streamable-http 网关强制要求两者都有），
并去掉下游的 ``Origin`` 头（代理是上游真正的客户端，下游 Origin 不应透传；否则 mock 上游会按
来源白名单回 403）。

工具白名单 ``LG_MCP_ALLOWED_TOOLS``（逗号分隔）同时作用于：
- ``tools/list``：响应（JSON 或 SSE）被缓冲后过滤，只把白名单内的工具交给 harness。
  真实 ERP 网关暴露约 2000 个工具（tools/list 约 5.8 MB），不过滤会撑爆 harness 上下文；
- ``tools/call``：白名单外的工具直接返回 JSON-RPC 错误，不转发。
``real`` 模式未设置白名单时拒绝启动（退出码 2），除非显式 ``LG_MCP_ALLOW_ALL=1``；
``mock`` 模式未设置白名单时全部透传。

协议兼容：上游对未知方法回 HTTP 500 且正文含 ``Missing handler for request type``（MCP Java SDK
的非标准行为）时，改写为标准 JSON-RPC ``-32601 Method not found``，让新协议客户端回退。

每次请求写一行 JSONL 到 ``$LG_MCP_LOG_DIR/requests.jsonl``（方法、工具名、耗时、状态、
过滤前后工具数；不记密钥、不记参数和返回正文）。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "content-length", "host",
              "accept-encoding"}
MCP_ACCEPT = "application/json, text/event-stream"
_log_lock = threading.Lock()
# 过滤后的 tools/list 结果：代理按 trial 起停，工具目录在 trial 内不变。
# 真实网关 tools/list 约 5.8 MB，实测单次 7–52 s；缓存后只付一次代价（Pi 默认单请求超时 60 s）。
_tools_cache: dict[tuple, tuple[bytes, str, int, int]] = {}
_tools_cache_lock = threading.Lock()


def _log(entry: dict) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry},
                      ensure_ascii=False)
    with _log_lock:
        log_dir = os.environ.get("LG_MCP_LOG_DIR")
        if log_dir:
            with open(Path(log_dir) / "requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(line, file=sys.stderr, flush=True)


def _target() -> tuple[str, dict]:
    """返回当前路由的 (上游 URL, 注入头)。"""
    target = os.environ.get("LG_MCP_TARGET", "mock").strip().lower()
    if target == "real":
        url = os.environ.get("LG_ERP_MCP_URL", "")
        headers = os.environ.get("LG_ERP_MCP_HEADERS", "")
    else:
        url = os.environ.get("LG_MCP_MOCK_URL", "")
        headers = os.environ.get("LG_MCP_MOCK_HEADERS", "")
    try:
        parsed = json.loads(headers) if headers else {}
    except json.JSONDecodeError:
        parsed = {}
    return url, (parsed if isinstance(parsed, dict) else {})


def _allowed_tools() -> set[str]:
    return {t.strip() for t in os.environ.get("LG_MCP_ALLOWED_TOOLS", "").split(",") if t.strip()}


def filter_tools_list(raw: bytes, content_type: str, allow: set[str]) -> tuple[bytes, int, int]:
    """按白名单过滤 tools/list 响应。返回 (新响应体, 过滤前工具数, 过滤后工具数)。

    兼容两种响应：``application/json``（单个 JSON-RPC 对象）和 ``text/event-stream``
    （``data:`` 行里是 JSON-RPC 对象）。解析失败时原样返回，计数为 -1。
    """
    def _filter(msg: dict) -> tuple[int, int]:
        tools = (msg.get("result") or {}).get("tools")
        if not isinstance(tools, list):
            return -1, -1
        kept = [t for t in tools if isinstance(t, dict) and t.get("name") in allow]
        msg["result"]["tools"] = kept
        return len(tools), len(kept)

    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        out_lines, before, after = [], -1, -1
        # 不能用 splitlines()：JSON 字符串里可能含 U+2028 等字符。
        for line in text.split("\n"):
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    out_lines.append(line)
                    continue
                if isinstance(msg, dict):
                    b, a = _filter(msg)
                    if b >= 0:
                        before, after = b, a
                out_lines.append("data: " + json.dumps(msg, ensure_ascii=False))
            else:
                out_lines.append(line)
        return "\n".join(out_lines).encode("utf-8"), before, after
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return raw, -1, -1
    if not isinstance(msg, dict):
        return raw, -1, -1
    before, after = _filter(msg)
    return json.dumps(msg, ensure_ascii=False).encode("utf-8"), before, after


def set_rpc_id(data: bytes, content_type: str, rpc_id) -> bytes:
    """把 JSON-RPC 响应（JSON 或 SSE）里的 id 改成 ``rpc_id``；解析失败时原样返回。"""
    text = data.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        lines = []
        for line in text.split("\n"):
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    lines.append(line)
                    continue
                if isinstance(msg, dict) and "result" in msg:
                    msg["id"] = rpc_id
                lines.append("data: " + json.dumps(msg, ensure_ascii=False))
            else:
                lines.append(line)
        return "\n".join(lines).encode("utf-8")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return data
    if isinstance(msg, dict):
        msg["id"] = rpc_id
    return json.dumps(msg, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lg-mcp-proxy"

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"ok": True, "target": os.environ.get("LG_MCP_TARGET", "mock")})
        elif self.path.rstrip("/") == "/mcp":
            self._forward()
        else:
            self._reply(404, {"error": {"message": f"lg-mcp-proxy: unknown path {self.path}"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") == "/mcp":
            self._forward()
        else:
            self._reply(404, {"error": {"message": f"lg-mcp-proxy: unknown path {self.path}"}})

    def do_DELETE(self) -> None:  # streamable-http 客户端结束会话
        if self.path.rstrip("/") == "/mcp":
            self._forward()
        else:
            self._reply(404, {"error": {"message": f"lg-mcp-proxy: unknown path {self.path}"}})

    def _send_cached_tools(self, cached: tuple[bytes, str, int, int], rpc_id, started) -> None:
        """回放缓存的过滤结果，并把 JSON-RPC id 改成本次请求的 id。"""
        data, content_type, before, after = cached
        body = set_rpc_id(data, content_type, rpc_id)
        self.send_response(200)
        self.send_header("content-type", content_type or "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _log({"event": "request", "http": self.command, "method": "tools/list", "tool": None,
              "upstream_host": "cache", "status": 200, "bytes": len(body),
              "ms": int((time.monotonic() - started) * 1000), "error": None,
              "tools_before": before, "tools_after": after})

    def _forward(self) -> None:
        started = time.monotonic()
        base, inject = _target()
        if not base:
            _log({"event": "misconfigured", "target": os.environ.get("LG_MCP_TARGET")})
            self._reply(503, {"error": {"message": "lg-mcp-proxy: upstream URL not set"}})
            return

        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        method = rpc_id = tool = None
        if body:
            payload = None
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                method = payload.get("method")
                rpc_id = payload.get("id")
                if method == "tools/call":
                    tool = (payload.get("params") or {}).get("name")
                    allow = _allowed_tools()
                    if allow and tool not in allow:
                        _log({"event": "tool_rejected", "tool": tool})
                        self._reply(200, {"jsonrpc": "2.0", "id": rpc_id,
                                          "error": {"code": -32601,
                                                    "message": f"lg-mcp-proxy: tool {tool!r} not allowed"}})
                        return

        target = urlsplit(base)
        # 透传客户端头，去掉 hop-by-hop、Accept（强制 MCP 双类型）与 Origin，叠加注入头。
        # 去掉 Origin：Origin 是最初客户端（浏览器语义）的来源标记，经本代理转发到另一来源的上游
        # 后已无意义；而 mock 上游（erp-mock）对非回环来源的 Origin 会按 DNS 重绑定防护回 403。
        # 代理才是上游真正的 HTTP 客户端，不应把下游 Origin 透传上去（真实网关对此不敏感，无副作用）。
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in ("accept", "origin")}
        headers["Accept"] = MCP_ACCEPT
        headers.update(inject)
        headers["Content-Length"] = str(len(body))
        conn_cls = (http.client.HTTPSConnection if target.scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(target.netloc, timeout=float(os.environ.get("LG_MCP_TIMEOUT", "120")))
        path = target.path + (f"?{target.query}" if target.query else "")
        allow = _allowed_tools()
        filter_list = method == "tools/list" and bool(allow)
        cache_key = (base, tuple(sorted(allow)))
        if filter_list and os.environ.get("LG_MCP_CACHE_TOOLS", "1") != "0":
            with _tools_cache_lock:
                cached = _tools_cache.get(cache_key)
            if cached is not None:
                self._send_cached_tools(cached, rpc_id, started)
                return
        status, n_bytes, error = None, 0, None
        tools_before = tools_after = None
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            if resp.status == 500 and rpc_id is not None and method:
                # 网关（MCP Java SDK）对未知方法返回 HTTP 500 + 非标准错误体；规范要求 JSON-RPC
                # -32601。新版客户端（如 DeepSeek Harness 先发 2026 协议的 server/discover）
                # 只在收到 -32601 时回退到 initialize，否则一直重试、注册不到工具。
                raw = resp.read()
                if b"Missing handler for request type" in raw:
                    status = 200
                    error = "rewritten_method_not_found"
                    self._reply(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {
                        "code": -32601, "message": f"Method not found: {method}"}})
                    return
                self._reply(502, {"error": {"message": "lg-mcp-proxy: upstream 500"}})
                return
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                # server/date 已由 send_response 写出，避免重复。
                if k.lower() not in HOP_BY_HOP and k.lower() not in ("server", "date"):
                    self.send_header(k, v)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            if filter_list and resp.status == 200:
                # tools/list 需要整体缓冲后过滤，不能流式透传。
                raw = resp.read()
                content_type = resp.getheader("content-type") or ""
                data, tools_before, tools_after = filter_tools_list(raw, content_type, allow)
                if tools_before >= 0:
                    with _tools_cache_lock:
                        _tools_cache[cache_key] = (data, content_type, tools_before, tools_after)
                n_bytes = len(data)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            else:
                while chunk := resp.read1(65536):
                    n_bytes += len(chunk)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as exc:
            error = type(exc).__name__
            if status is None:
                self._reply(502, {"error": {"message": "lg-mcp-proxy: upstream error"}})
        finally:
            conn.close()
            entry = {"event": "request", "http": self.command, "method": method, "tool": tool,
                     "upstream_host": target.netloc, "status": status, "bytes": n_bytes,
                     "ms": int((time.monotonic() - started) * 1000), "error": error}
            if tools_before is not None:
                entry.update(tools_before=tools_before, tools_after=tools_after)
            _log(entry)


def main() -> None:
    port = int(os.environ.get("LG_MCP_PORT", "18090"))
    log_dir = os.environ.get("LG_MCP_LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    url, _ = _target()
    target = os.environ.get("LG_MCP_TARGET", "mock")
    allow = sorted(_allowed_tools())
    if (target.strip().lower() == "real" and not allow
            and os.environ.get("LG_MCP_ALLOW_ALL") != "1"):
        # 真实 ERP 网关约 2000 个工具，其中大量写操作；默认拒绝全量暴露。
        _log({"event": "fatal", "message": "LG_MCP_TARGET=real requires LG_MCP_ALLOWED_TOOLS "
              "(or LG_MCP_ALLOW_ALL=1 to expose every upstream tool)"})
        sys.exit(2)
    _log({"event": "listening", "port": port, "target": target,
          "upstream_host": urlsplit(url).netloc, "allowed_tools": allow})
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
