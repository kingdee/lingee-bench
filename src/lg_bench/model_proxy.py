"""每个 trial 的模型代理 sidecar（只用标准库）。

运行在独立容器 ``lg-model-proxy`` 中，与 agent 容器（``main``）同属一个 compose 项目。
真实端点和密钥只存在于本容器的环境变量；agent 侧只拿到占位密钥。

路由（按路径前缀）：
- ``/openai/<rest>``    → ``$LG_OPENAI_BASE_URL/<rest>``，``Authorization: Bearer $LG_OPENAI_API_KEY``
- ``/anthropic/<rest>`` → ``$LG_ANTHROPIC_BASE_URL/<rest>``，``x-api-key: $LG_ANTHROPIC_API_KEY``
- ``/health``           → 200

请求体里的 ``model`` 必须在 ``LG_PROXY_ALLOWED_MODELS`` 内（允许带 ``lg-proxy/`` 路由前缀，
转发前去掉前缀改写为真实模型名）；其他模型一律 400，
防止 harness 静默切换模型。每次请求写一行 JSONL 到 ``$LG_PROXY_LOG_DIR/requests.jsonl``，
不记录密钥和请求正文。响应（含 SSE 流）按块原样回传。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROUTE_PREFIX = "lg-proxy/"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "content-length", "host",
              "accept-encoding"}
UPSTREAMS = {
    "openai": ("LG_OPENAI_BASE_URL", "LG_OPENAI_API_KEY"),
    "anthropic": ("LG_ANTHROPIC_BASE_URL", "LG_ANTHROPIC_API_KEY"),
}

_log_lock = threading.Lock()


def _log(entry: dict) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry},
                      ensure_ascii=False)
    with _log_lock:
        log_dir = os.environ.get("LG_PROXY_LOG_DIR")
        if log_dir:
            with open(Path(log_dir) / "requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(line, file=sys.stderr, flush=True)


def _allowed_models() -> set[str]:
    return {m.strip() for m in os.environ.get("LG_PROXY_ALLOWED_MODELS", "").split(",") if m.strip()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lg-model-proxy"

    def log_message(self, *args) -> None:  # 用自己的 JSONL 日志
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
            self._reply(200, {"ok": True})
        else:
            self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        started = time.monotonic()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        request_id = uuid.uuid4().hex
        name, _, rest = self.path.lstrip("/").partition("/")
        if name not in UPSTREAMS:
            self._reply(404, {"error": {"message": f"lg-proxy: unknown route /{name}"}})
            return
        url_env, key_env = UPSTREAMS[name]
        base, key = os.environ.get(url_env, ""), os.environ.get(key_env, "")
        if not base or not key:
            _log({"event": "misconfigured", "route": name})
            self._reply(503, {"error": {"message": f"lg-proxy: {url_env}/{key_env} not set"}})
            return

        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        requested = upstream_model = None
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "model" in payload:
                requested = payload["model"]
                upstream_model = str(requested).removeprefix(ROUTE_PREFIX)
                if upstream_model not in _allowed_models():
                    _log({"event": "rejected", "route": name, "path": self.path,
                          "requested_model": requested})
                    self._reply(400, {"error": {"message":
                                                f"lg-proxy: model {requested!r} not allowed"}})
                    return
                payload["model"] = upstream_model
                body = json.dumps(payload, ensure_ascii=False).encode()

        target = urlsplit(base.rstrip("/") + "/" + rest)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
                   and k.lower() not in ("authorization", "x-api-key")}
        if name == "openai":
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["x-api-key"] = key
        headers["Content-Length"] = str(len(body))
        conn_cls = (http.client.HTTPSConnection if target.scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(target.netloc, timeout=float(os.environ.get("LG_PROXY_TIMEOUT", "900")))
        path = target.path + (f"?{target.query}" if target.query else "")
        status, n_bytes, error = None, 0, None
        phase, complete = "upstream_connect", False
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            phase = "upstream_headers"
            resp = conn.getresponse()
            status = resp.status
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            while True:
                phase = "upstream_read"
                chunk = resp.read1(65536)
                if not chunk:
                    break
                phase = "downstream_write"
                n_bytes += len(chunk)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            complete = True
        except Exception as exc:  # 上游或客户端断开
            error = type(exc).__name__
            # A failed chunked response must end with a broken connection,
            # never a success terminator or an open keep-alive socket. Otherwise
            # the client waits for a chunk while this handler waits for its next
            # HTTP request. Closing preserves the transport failure for all SDKs.
            self.close_connection = True
            if status is None:
                self._reply(502, {"error": {"message": "lg-proxy: upstream error"}})
        finally:
            conn.close()
            _log({"event": "request", "route": name, "path": self.path, "method": self.command,
                  "request_id": request_id, "started_at": started_at,
                  "response_complete": complete, "error_phase": phase if error else None,
                  "requested_model": requested, "upstream_model": upstream_model,
                  "upstream_host": target.netloc, "status": status, "bytes": n_bytes,
                  "ms": int((time.monotonic() - started) * 1000), "error": error})


def main() -> None:
    port = int(os.environ.get("LG_PROXY_PORT", "18080"))
    log_dir = os.environ.get("LG_PROXY_LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    configured = {name: bool(os.environ.get(u) and os.environ.get(k))
                  for name, (u, k) in UPSTREAMS.items()}
    _log({"event": "listening", "port": port, "allowed_models": sorted(_allowed_models()),
          "upstreams_configured": configured,
          "upstream_hosts": {n: urlsplit(os.environ.get(u, "")).netloc
                             for n, (u, _) in UPSTREAMS.items()}})
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
