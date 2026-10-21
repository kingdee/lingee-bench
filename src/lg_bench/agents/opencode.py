"""OpenCode：沿用 Harbor 0.22.0 内置 ``opencode`` adapter，把模型连接改到代理 sidecar。

内置 adapter 负责安装（nvm + ``npm i -g opencode-ai@<version>``）、
``opencode run --format=json`` 调用、原生 resume 和 ATIF 轨迹转换。
多轮 job 显式设置 ``resume_trajectory: true``，由 Harbor 在后轮调用原生恢复；
本类不另行重建对话历史。上游要求 ``model_name`` 为 ``provider/model``。本类：

- 在 ``~/.config/opencode/opencode.json`` 注册自定义 provider ``lg-openai``
  （``@ai-sdk/openai-compatible``，``baseURL`` 指向代理 ``/openai`` 路由，``apiKey`` 为占位值）；
- 把 ``model`` 与 ``small_model`` 都锁定为该 provider 下的同一模型，关闭自动更新与分享；
- job YAML 中仍写裸模型 id（``glm-5.2``），与其他 harness 一致。

安装方式（``install_mode``，见 docs/harness-images.md）：``image`` 使用只读挂载的
``/opt/lg-harness/opencode``；内置 run 命令以 ``. ~/.nvm/nvm.sh;`` 开头，因此在该文件中把镜像的
``bin/`` 加入 PATH（文件不存在时创建）；不执行 apt / nvm / npm。``online`` 为内置在线安装；
``auto``（默认）已挂载则 image，否则 online。

上游依据：https://opencode.ai/docs/providers/ （自定义 OpenAI 兼容 provider）、
https://opencode.ai/docs/config/ 。``name()`` 仍返回 ``opencode``。
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any, ClassVar

from harbor.agents.installed.opencode import OpenCode
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.environments.base import BaseEnvironment

from lg_bench.agents._base import (API_KEY_ENV, BASE_URL_ENV, OPENAI_PROXY_BASE,
                                   PLACEHOLDER_KEY)
from lg_bench.agents._image import HarnessImageMixin
from lg_bench.agents._mcp import _check_name

PROVIDER_ID = "lg-openai"
OUTPUT_TOKEN_LIMIT_ENV = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"


class LgOpenCode(HarnessImageMixin, OpenCode):
    HARNESS_ID: ClassVar[str] = "opencode"
    PINNED_VERSION: ClassVar[str] = "1.19.0"
    # 真实端点/密钥只在 Harbor 进程环境中，供代理 sidecar 插值。
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = (BASE_URL_ENV, API_KEY_ENV)

    def __init__(self, logs_dir, *args, model_name: str | None = None,
                 version: str | None = None,
                 opencode_config: dict[str, Any] | None = None,
                 install_mode: str | None = None,
                 max_output_tokens: int = 65535, **kwargs):
        if not model_name:
            raise ValueError("opencode: model_name is required (e.g. glm-5.2)")
        if "/" in model_name:
            raise ValueError("opencode: use the bare model id, not provider/model")
        if (isinstance(max_output_tokens, bool)
                or not isinstance(max_output_tokens, int)
                or max_output_tokens <= 0):
            raise ValueError("opencode: max_output_tokens must be a positive integer")
        self._max_output_tokens = max_output_tokens
        self.bare_model = model_name
        qualified = f"{PROVIDER_ID}/{model_name}"
        config = self._lg_config(model_name, qualified)
        if opencode_config:
            self._deep_merge(config, copy.deepcopy(opencode_config))
        super().__init__(logs_dir, *args, model_name=qualified,
                         version=version or self.PINNED_VERSION,
                         opencode_config=config, **kwargs)
        self._init_install_mode(install_mode)

    @property
    def model_connection(self) -> ResolvedModelConnection:
        # Harbor's native run/resume copies this env for every CLI invocation.
        # OpenCode 1.19.0 ignores model-level maxOutputTokens in its JSON config.
        connection = super().model_connection
        return replace(connection, env={
            **connection.env,
            OUTPUT_TOKEN_LIMIT_ENV: str(self._max_output_tokens),
        })

    async def install(self, environment: BaseEnvironment) -> None:
        await self._install_with_mode(environment)

    async def _online_install(self, environment: BaseEnvironment) -> None:
        await OpenCode.install(self, environment)

    def _build_register_config_command(self) -> str | None:
        """内置实现只写 MCP 的 ``type``/``url``；这里校验 server 名，与其他 harness 保持一致。"""
        for server in self.mcp_servers:
            _check_name(server.name, self.HARNESS_ID)
        return super()._build_register_config_command()

    async def _link_from_image(self, environment: BaseEnvironment) -> None:
        line = 'export PATH="/opt/lg-harness/opencode/bin:$PATH"'
        await self.exec_as_agent(environment, command=(
            "set -e; mkdir -p ~/.nvm; "
            f"grep -qsF '{line}' ~/.nvm/nvm.sh || echo '{line}' >> ~/.nvm/nvm.sh; "
            f". ~/.nvm/nvm.sh; opencode --version | grep -qx '{self._version}'"
        ))

    @staticmethod
    def _lg_config(model: str, qualified: str) -> dict[str, Any]:
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "lg-bench model proxy",
                "options": {"baseURL": OPENAI_PROXY_BASE, "apiKey": PLACEHOLDER_KEY},
                "models": {model: {"name": model}},
            }},
            # 主模型与标题/摘要用的小模型锁定为同一个，禁止静默切换。
            "model": qualified,
            "small_model": qualified,
            "autoupdate": False,
            "share": "disabled",
        }

    @classmethod
    def required_env(cls) -> tuple[str, ...]:
        return cls.REQUIRED_ENV

    def dry_run_plan(self, instruction: str = "<instruction.md>") -> dict[str, Any]:
        config = self._deep_merge(copy.deepcopy(self._DEFAULT_CONFIG), {})
        config = self._deep_merge(config, copy.deepcopy(self._opencode_config))
        return {
            "install": (f"install_mode={self.install_mode}：image 模式挂载 /opt/lg-harness/opencode；"
                        f"online 模式 npm i -g opencode-ai@{self._version}（Harbor 内置 install）"),
            "config_files": {"~/.config/opencode/opencode.json": json.dumps(config, indent=2)},
            "run_env": {OUTPUT_TOKEN_LIMIT_ENV: str(self._max_output_tokens)},
            "run": (f"opencode --model={self.model_name} run --format=json --thinking "
                    f"--dangerously-skip-permissions -- '{instruction}' | tee /logs/agent/opencode.txt"),
        }
