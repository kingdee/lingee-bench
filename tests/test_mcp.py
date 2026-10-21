"""MCP 代理与各 harness adapter 的 MCP 配置渲染（不启动容器、不调用模型）。"""

import json
from pathlib import Path

import pytest
from harbor.models.task.config import MCPServerConfig

from lg_bench import mcp_proxy
from lg_bench.agents.deepseek_harness import DeepSeekHarness
from lg_bench.agents.opencode import LgOpenCode
from lg_bench.agents.pi import LgPi

PROXY = "http://lg-mcp-proxy:18090/mcp"
SERVERS = [MCPServerConfig(name="erp", transport="streamable-http", url=PROXY)]


def _no_secret(text: str) -> None:
    for word in ("Authorization", "access_token", "Bearer"):
        assert word not in text


def test_pi_user_level_direct_exposure(tmp_path):
    agent = LgPi(tmp_path, model_name="glm-5.2", mcp_servers=SERVERS)
    cfg = json.loads(agent.build_config_files()["$HOME/.pi/agent/mcp.json"])
    assert cfg == {"mcpServers": {"erp": {"type": "streamable-http", "url": PROXY,
                                          "exposure": "direct"}}}


def test_pi_rejects_sse(tmp_path):
    sse = [MCPServerConfig(name="erp", transport="sse", url="http://x/sse")]
    with pytest.raises(ValueError, match="sse"):
        LgPi(tmp_path, model_name="glm-5.2", mcp_servers=sse).build_config_files()


def test_deepseek_inserts_mcp_client_plugin(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name="glm-5.2", mcp_servers=SERVERS)
    patch = next(iter(agent.build_config_files().values()))
    assert "- insert:" in patch
    assert "name: '@deepseek-ai/dsh-mcp-client'" in patch
    assert 'serverName: "erp"' in patch
    assert "transport: streamable-http" in patch
    assert f'url: "{PROXY}"' in patch


def test_deepseek_stdio(tmp_path):
    stdio = [MCPServerConfig(name="mock", transport="stdio", command="python3",
                             args=["/opt/mcp/server.py"])]
    patch = next(iter(DeepSeekHarness(tmp_path, model_name="glm-5.2",
                                      mcp_servers=stdio).build_config_files().values()))
    assert "transport: stdio" in patch and '["/opt/mcp/server.py"]' in patch


def test_opencode_remote(tmp_path):
    agent = LgOpenCode(tmp_path, model_name="glm-5.2", mcp_servers=SERVERS)
    command = agent._build_register_config_command()
    assert '"type": "remote"' in command and PROXY in command


def test_invalid_server_name_rejected(tmp_path):
    bad = [MCPServerConfig(name="erp server", transport="streamable-http", url=PROXY)]
    with pytest.raises(ValueError, match="A-Za-z0-9"):
        LgPi(tmp_path, model_name="glm-5.2", mcp_servers=bad).build_config_files()


# ---- 代理：tools/list 白名单过滤 ----------------------------------------------------

TOOLS = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
    {"name": "keep_me", "description": "a\u2028b"}, {"name": "drop_me"}, {"name": "also_keep"}]}}


def test_filter_tools_list_sse():
    raw = ("event: message\ndata: " + json.dumps(TOOLS, ensure_ascii=False) + "\n\n").encode()
    out, before, after = mcp_proxy.filter_tools_list(raw, "text/event-stream;charset=utf-8",
                                                     {"keep_me", "also_keep"})
    assert (before, after) == (3, 2)
    data = next(line for line in out.decode().split("\n") if line.startswith("data:"))
    names = [t["name"] for t in json.loads(data[5:])["result"]["tools"]]
    assert names == ["keep_me", "also_keep"]
    assert out.decode().startswith("event: message\n")


def test_filter_tools_list_json():
    out, before, after = mcp_proxy.filter_tools_list(json.dumps(TOOLS).encode(),
                                                     "application/json", {"drop_me"})
    assert (before, after) == (3, 1)
    assert [t["name"] for t in json.loads(out)["result"]["tools"]] == ["drop_me"]


def test_filter_tools_list_unparseable_passthrough():
    out, before, _ = mcp_proxy.filter_tools_list(b"not json", "application/json", {"x"})
    assert out == b"not json" and before == -1


def test_set_rpc_id_sse_and_json():
    sse = ("event: message\ndata: " + json.dumps(TOOLS) + "\n\n").encode()
    out = mcp_proxy.set_rpc_id(sse, "text/event-stream", 42)
    data = next(line for line in out.decode().split("\n") if line.startswith("data:"))
    assert json.loads(data[5:])["id"] == 42
    assert json.loads(mcp_proxy.set_rpc_id(json.dumps(TOOLS).encode(),
                                           "application/json", "abc"))["id"] == "abc"


def test_compose_keeps_secrets_in_sidecar():
    text = Path("configs/compose/mcp-proxy.yaml").read_text()
    assert "LG_ERP_MCP_HEADERS" in text
    main = text.split("  main:")[1]
    assert "LG_ERP_MCP" not in main


# ---- 代理 HTTP 行为：假上游模拟 MCP 网关 ------------------------------------------------

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _FakeGateway(BaseHTTPRequestHandler):
    """模拟 bos-mcp-gateway：未知方法回 HTTP 500 + Java SDK 错误体；tools/list 回 SSE。"""
    calls: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).calls.append((body["method"], self.headers.get("x-secret"),
                                 self.headers.get("accept")))
        if body["method"] == "tools/list":
            data = ("event: message\ndata: " + json.dumps(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [
                    {"name": "keep_me"}, {"name": "drop_me"}]}}) + "\n\n").encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream;charset=utf-8")
        else:
            data = json.dumps({"jsonRpcError": {"code": -32603, "message":
                               "Failed to handle request: Missing handler for request type: "
                               + body["method"]}}).encode()
            self.send_response(500)
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    # 每个用例独立的上游类与调用记录，避免上一个用例尚未退出的处理线程串写。
    gateway = type("Gateway", (_FakeGateway,), {"calls": []})
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), gateway)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setenv("LG_MCP_TARGET", "real")
    monkeypatch.setenv("LG_ERP_MCP_URL", f"http://127.0.0.1:{upstream.server_port}/mcp")
    monkeypatch.setenv("LG_ERP_MCP_HEADERS", json.dumps({"x-secret": "s3cr3t"}))
    monkeypatch.setenv("LG_MCP_ALLOWED_TOOLS", "keep_me")
    monkeypatch.setenv("LG_MCP_LOG_DIR", str(tmp_path))
    mcp_proxy._tools_cache.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mcp_proxy.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", tmp_path, gateway
    server.shutdown()
    upstream.shutdown()


def _post(url, body, accept="application/json"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": accept})
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.status, resp.read().decode()


def test_proxy_rewrites_missing_handler_to_method_not_found(proxy):
    url, _, _ = proxy
    status, text = _post(url, {"jsonrpc": "2.0", "id": 7, "method": "server/discover"})
    assert status == 200
    assert json.loads(text) == {"jsonrpc": "2.0", "id": 7,
                                "error": {"code": -32601,
                                          "message": "Method not found: server/discover"}}


def test_proxy_injects_secret_forces_accept_and_filters(proxy):
    url, logs, gateway = proxy
    for rpc_id in (1, 2):  # 第二次命中缓存
        status, text = _post(url, {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/list"})
        data = next(line for line in text.split("\n") if line.startswith("data:"))
        msg = json.loads(data[5:])
        assert status == 200 and msg["id"] == rpc_id
        assert [t["name"] for t in msg["result"]["tools"]] == ["keep_me"]
    # 只打到上游一次；密钥由代理注入；Accept 被强制为 MCP 双类型
    assert gateway.calls == [("tools/list", "s3cr3t", "application/json, text/event-stream")]
    # 日志在响应发出后才写，给处理线程一点时间落盘。
    import time
    log = ""
    for _ in range(50):
        log = (logs / "requests.jsonl").read_text()
        if '"upstream_host": "cache"' in log:
            break
        time.sleep(0.02)
    assert '"upstream_host": "cache"' in log
    assert "s3cr3t" not in log


def test_proxy_blocks_tool_outside_allowlist(proxy):
    url, _, gateway = proxy
    _, text = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "drop_me", "arguments": {}}})
    assert json.loads(text)["error"]["code"] == -32601
    assert gateway.calls == []  # 没有转发到上游


# ---- 预检：CI 默认集覆盖子目录里的 ERP MCP 冒烟配置 --------------------------------

def test_default_preflight_covers_erp_mcp_mock_smoke_jobs():
    from lg_bench.preflight import default_configs

    configs = [str(p) for p in default_configs()]
    erp = [c for c in configs if "/erp-mcp-mock-smoke/" in c]
    assert len(erp) == 4
    assert not any(c.endswith(".local.yaml") for c in configs)


def test_preflight_requires_mcp_proxy_sidecar(tmp_path):
    import yaml
    from lg_bench.preflight import check_config

    src = Path("configs/jobs/erp-mcp-mock-smoke/opencode-glm-5.2-erp-mock-smoke.yaml")
    ok = check_config(src, dry_run=False, logs_dir=tmp_path, check_local_images=False)
    assert ok[0]["errors"] == []
    job = yaml.safe_load(src.read_text())
    job["environment"]["extra_docker_compose"].remove("configs/compose/mcp-proxy.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(job, allow_unicode=True))
    rows = check_config(broken, dry_run=False, logs_dir=tmp_path, check_local_images=False)
    assert "environment.extra_docker_compose must include configs/compose/mcp-proxy.yaml" \
        in rows[0]["errors"]


def test_preflight_checks_local_images_by_default(tmp_path, monkeypatch):
    from lg_bench import harness_images
    from lg_bench.preflight import check_config

    monkeypatch.setattr(harness_images, "compose_is_current", lambda name: True)
    monkeypatch.setattr(harness_images, "docker_available", lambda: True)
    monkeypatch.setattr(harness_images, "check_local", lambda name: {"errors": ["missing image"]})
    src = Path("configs/jobs/opencode-glm-5.2-smoke.yaml")
    rows = check_config(src, dry_run=False, logs_dir=tmp_path)
    assert "harness image: missing image" in rows[0]["errors"]


def test_preflight_without_local_images_still_checks_compose_lock(tmp_path, monkeypatch):
    from lg_bench import harness_images
    from lg_bench.preflight import check_config

    def unexpected_docker_call():
        raise AssertionError("static preflight must not query local harness images")

    monkeypatch.setattr(harness_images, "compose_is_current", lambda name: False)
    monkeypatch.setattr(harness_images, "docker_available", unexpected_docker_call)
    src = Path("configs/jobs/opencode-glm-5.2-smoke.yaml")
    rows = check_config(src, dry_run=False, logs_dir=tmp_path, check_local_images=False)
    assert any("与锁文件不一致" in error for error in rows[0]["errors"])
    assert rows[0]["warnings"]
