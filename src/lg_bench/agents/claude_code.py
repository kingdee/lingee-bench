"""Claude Code：沿用 Harbor 内置 adapter，只改模型连接与安装方式。

模型连接：Harbor 0.18.0 的 ``ClaudeCode.run()`` 从 ``os.environ`` 读 ``ANTHROPIC_BASE_URL``，
从 ``_get_env``（agent extra_env 优先）读 ``ANTHROPIC_API_KEY``，没有可覆盖的钩子。因此本类
把 ``ANTHROPIC_API_KEY`` 放进 extra_env（占位密钥），并在 Harbor 进程内把 ``ANTHROPIC_BASE_URL``
设为代理地址（所有 trial 取值相同）。真实端点与密钥用 ``LG_ANTHROPIC_*`` 提供，只进入代理容器。

安装方式（``install_mode``，见 docs/harness-images.md）：
- ``image``：使用只读挂载的 ``/opt/lg-harness/claude-code``，软链 ``claude`` 到 ``~/.local/bin``，
  任务镜像缺少 ``ps/pgrep/pkill`` 时软链镜像自带的 procps 包装；不执行 apt / curl。
- ``online``：内置的在线安装（apt 安装 curl、procps + 官方 bootstrap 脚本）。
- ``auto``（默认）：已挂载则 image，否则 online。

``name()`` 仍返回 ``claude-code``，结果可与历史运行对比。
"""

from __future__ import annotations

import os
from typing import ClassVar

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment

from lg_bench.agents._base import ANTHROPIC_PROXY_BASE, PLACEHOLDER_KEY
from lg_bench.agents._image import HarnessImageMixin

MOUNT = "/opt/lg-harness/claude-code"


class LgClaudeCode(HarnessImageMixin, ClaudeCode):
    HARNESS_ID: ClassVar[str] = "claude-code"
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ("LG_ANTHROPIC_BASE_URL", "LG_ANTHROPIC_API_KEY")

    def __init__(self, logs_dir, *args, extra_env: dict[str, str] | None = None,
                 install_mode: str | None = None, **kwargs):
        extra_env = {**(extra_env or {}), "ANTHROPIC_API_KEY": PLACEHOLDER_KEY}
        extra_env.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ["ANTHROPIC_BASE_URL"] = ANTHROPIC_PROXY_BASE
        super().__init__(logs_dir, *args, extra_env=extra_env, **kwargs)
        self._init_install_mode(install_mode)

    @classmethod
    def required_env(cls) -> tuple[str, ...]:
        return cls.REQUIRED_ENV

    async def install(self, environment: BaseEnvironment) -> None:
        await self._install_with_mode(environment)

    async def _online_install(self, environment: BaseEnvironment) -> None:
        await ClaudeCode.install(self, environment)

    async def _link_from_image(self, environment: BaseEnvironment) -> None:
        await self.exec_as_agent(environment, command=(
            "set -e; mkdir -p ~/.local/bin; "
            f"ln -sf {MOUNT}/bin/claude ~/.local/bin/claude; "
            # 与在线安装一致：CC 清理进程树需要 ps/pgrep；任务镜像没有时用镜像自带的包装
            "for t in ps pgrep pkill; do "
            f"command -v $t >/dev/null 2>&1 || ln -sf {MOUNT}/tools/bin/$t ~/.local/bin/$t; done; "
            "grep -qs 'HOME/.local/bin' ~/.bashrc || "
            "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc; "
            'export PATH="$HOME/.local/bin:$PATH"; '
            f"claude --version | grep -q '^{self._version} '"
        ))

    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; claude --version'
