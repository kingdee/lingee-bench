"""Pi coding agent（earendil-works/pi）的 Harbor adapter。

Harbor 0.18.0 内置的 ``pi`` adapter 仍安装旧包名 ``@mariozechner/pi-coding-agent``，
且只为预置 provider 透传凭据，无法接自定义 OpenAI 兼容端点。本类复用其 JSON
事件 usage 解析，改为安装当前包 ``@earendil-works/pi-coding-agent`` 并写入自定义
provider 的 ``models.json``。

上游依据：
- 安装 https://github.com/earendil-works/pi （Node >= 22.19）
- 自定义端点 https://pi.dev/docs/latest/models （``models.json`` 的 providers/baseUrl/api/
  apiKey，apiKey 支持 ``$NAME`` 环境变量插值）

状态：配置就绪但未实跑。``~/.pi/agent/models.json`` 路径来自包配置
``piConfig.configDir=.pi`` 与上游文档，需首次真实运行确认。
"""

from __future__ import annotations

import json
import shlex
from typing import override

from harbor.agents.installed.pi import Pi as HarborPi
from harbor.models.agent.context import AgentContext

from lg_bench.agents._base import (AGENT_LOG_DIR, API_KEY_ENV, NVM_LOAD,
                                   LgInstalledAgent)
from lg_bench.agents._mcp import dumps, mcp_servers_json

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
# Pi 默认把 MCP 工具设为 codemode（不直接声明给模型）。评测用的是白名单后的少量工具，
# 设为 direct，与其他 harness 一样把工具直接声明给模型（见 Pi docs/mcp.md「Control tool exposure」）。
MCP_EXPOSURE = "direct"


class LgPi(LgInstalledAgent):
    HARNESS_ID = "pi"
    PINNED_VERSION = "1.1.0"
    OUTPUT_FILENAME = "pi.txt"
    _OUTPUT_FILENAME = OUTPUT_FILENAME  # 内置 HarborPi 解析器读取的属性名
    PROVIDER_ID = "lg-openai"

    def __init__(self, logs_dir, *args, thinking: str | None = None, **kwargs):
        if thinking is not None and thinking not in THINKING_LEVELS:
            raise ValueError(f"pi: thinking must be one of {THINKING_LEVELS}")
        self._thinking = thinking
        super().__init__(logs_dir, *args, **kwargs)
        if self._max_turns is not None:
            raise ValueError("pi: max_turns is not supported by the Pi CLI; rely on the task timeout")

    @staticmethod
    @override
    def name() -> str:
        return "lg-pi"

    @override
    def get_version_command(self) -> str | None:
        return f"{NVM_LOAD}; pi --version </dev/null"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    @override
    def build_install_command(self) -> str:
        return ("set -euo pipefail; "
                f"{self.node_install_snippet()} && "
                f"npm install -g --ignore-scripts @earendil-works/pi-coding-agent@{self._version} && "
                "pi --version")

    @override
    def build_config_files(self, *, placeholder: bool = False) -> dict[str, str]:
        models = {"providers": {self.PROVIDER_ID: {
            "baseUrl": self.base_url(placeholder=placeholder),
            "api": "openai-completions",
            # Pi 在请求时插值，文件内只保存变量名。
            "apiKey": "$" + API_KEY_ENV,
            "models": [{"id": self.model_name}],
        }}}
        files = {"$HOME/.pi/agent/models.json": json.dumps(models, indent=2) + "\n"}
        if self.mcp_servers:
            # 用户级配置无需项目信任；Pi 不支持 legacy SSE。
            files["$HOME/.pi/agent/mcp.json"] = dumps(mcp_servers_json(
                self.mcp_servers, harness=self.HARNESS_ID, http_type="streamable-http",
                allow_sse=False, extra={"exposure": MCP_EXPOSURE}))
        return files

    @override
    def build_run_command(self, instruction: str) -> str:
        parts = [f"{NVM_LOAD};", "pi", "--print", "--mode", "json", "--no-session",
                 "--provider", self.PROVIDER_ID, "--model", shlex.quote(self.model_name)]
        if self._thinking:
            parts += ["--thinking", self._thinking]
        parts.append(instruction)
        # 与 Harbor 内置 adapter 一致：去掉逐 token 的 message_update，保留其余事件。
        return (" ".join(parts) + " 2>&1 </dev/null"
                + " | grep --line-buffered -v '\"type\":\"message_update\"'"
                + f" | stdbuf -oL tee {AGENT_LOG_DIR}/{self.OUTPUT_FILENAME}")

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        # 事件格式与内置 adapter 相同（message_end.usage），直接复用其解析。
        HarborPi.populate_context_post_run(self, context)
