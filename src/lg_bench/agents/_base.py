"""三个自建 adapter 的公共部分：Node 安装、配置文件写入、模型代理接入和 dry-run 渲染。

模型请求一律经每个 trial 的代理 sidecar（``configs/compose/model-proxy.yaml``，
实现见 ``lg_bench/model_proxy.py``）。agent 容器里只有代理地址和占位密钥；
真实端点与密钥由 Harbor 进程环境（``--env-file``）经 compose 插值只注入代理容器。

所有面向容器的命令都由 ``build_*`` 方法生成纯字符串，``install``/``run`` 只负责执行，
这样 ``lg-bench preflight --dry-run`` 可以在不启动 Docker、不调用模型的前提下展示
实际会执行的命令（密钥一律脱敏）。
"""

from __future__ import annotations

import base64
import shlex
from pathlib import PurePosixPath
from typing import Any, ClassVar

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment

from lg_bench.agents._image import HarnessImageMixin
from harbor.models.agent.context import AgentContext

# Harbor 进程侧（代理容器）的真实端点与密钥变量名；agent 容器内永远拿不到真实值。
BASE_URL_ENV = "LG_OPENAI_BASE_URL"
API_KEY_ENV = "LG_OPENAI_API_KEY"
# agent 容器内看到的代理地址与占位密钥。
PROXY_URL = "http://lg-model-proxy:18080"
OPENAI_PROXY_BASE = f"{PROXY_URL}/openai"
ANTHROPIC_PROXY_BASE = f"{PROXY_URL}/anthropic"
PLACEHOLDER_KEY = "lg-proxy-placeholder"
MODEL_PROXY_COMPOSE = "configs/compose/model-proxy.yaml"
REDACTED = "***"
NVM_LOAD = '. "$HOME/.nvm/nvm.sh"'
# 容器内 agent 日志目录，由 Harbor 回收到 trial 的 agent/ 下。
AGENT_LOG_DIR = "/logs/agent"


class LgInstalledAgent(HarnessImageMixin, BaseInstalledAgent):
    """lg-bench 自建 adapter 的基类。子类只描述 harness 的安装、配置与调用方式。

    安装支持 ``install_mode``（见 docs/harness-images.md）：``image`` 用只读挂载的
    ``/opt/lg-harness/<name>``；``online`` 用 nvm+npm 在线安装；``auto``（默认）二选一。
    """

    HARNESS_ID: ClassVar[str]
    PINNED_VERSION: ClassVar[str]
    OUTPUT_FILENAME: ClassVar[str]
    NODE_MAJOR: ClassVar[int] = 22
    # Harbor 进程环境中必须存在的变量（供代理 sidecar 插值）；不会传入 agent 容器。
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = (BASE_URL_ENV, API_KEY_ENV)

    def __init__(self, logs_dir, *args, version: str | None = None,
                 max_turns: int | None = None, install_mode: str | None = None, **kwargs):
        self._max_turns = int(max_turns) if max_turns is not None else None
        super().__init__(logs_dir, *args, version=version or self.PINNED_VERSION, **kwargs)
        self._init_install_mode(install_mode)
        if not self.model_name:
            raise ValueError(f"{self.HARNESS_ID}: model_name is required (e.g. glm-5.2)")
        if "/" in self.model_name:
            raise ValueError(f"{self.HARNESS_ID}: use the bare model id, not provider/model")

    @classmethod
    def required_env(cls) -> tuple[str, ...]:
        return cls.REQUIRED_ENV

    # ---- 端点 ---------------------------------------------------------------
    def base_url(self, *, placeholder: bool = False) -> str:
        """agent 侧的 OpenAI 兼容 base URL：始终是代理 sidecar。"""
        return OPENAI_PROXY_BASE

    def missing_env(self) -> list[str]:
        return [key for key in self.required_env() if not self._get_env(key)]

    def harness_env(self) -> dict[str, str]:
        """harness 自身的非密钥环境变量（关闭遥测、自动更新等）。"""
        return {}

    def build_run_env(self, *, redact: bool = False) -> dict[str, str]:
        env = dict(self.harness_env())
        # 配置文件里引用 $LG_OPENAI_API_KEY 的 harness 拿到的是占位值；真实密钥在代理容器。
        env[API_KEY_ENV] = PLACEHOLDER_KEY
        return env

    # ---- 生成命令 ---------------------------------------------------------
    def build_install_command(self) -> str:
        raise NotImplementedError

    def build_config_files(self, *, placeholder: bool = False) -> dict[str, str]:
        """{容器内路径: 文件内容}；路径可以以 $HOME 开头。内容不得包含密钥。"""
        return {}

    def build_run_command(self, instruction: str) -> str:
        raise NotImplementedError

    def node_install_snippet(self) -> str:
        return nvm_node_install_snippet(self.NODE_MAJOR)

    @staticmethod
    def write_file_command(path: str, content: str) -> str:
        # base64 传输，避免 JSON/YAML 内容在 shell 中被二次解释。
        encoded = base64.b64encode(content.encode()).decode()
        parent = str(PurePosixPath(path).parent)
        return (f'mkdir -p "{parent}" && echo {encoded} | base64 -d > "{path}" '
                f'&& chmod 600 "{path}"')

    def dry_run_plan(self, instruction: str = "<instruction.md>") -> dict[str, Any]:
        return {
            "install": self.build_install_command(),
            "config_files": self.build_config_files(placeholder=True),
            "run_env": self.build_run_env(redact=True),
            "run": self.build_run_command(shlex.quote(instruction)),
        }

    # ---- Harbor 生命周期 --------------------------------------------------
    async def install(self, environment: BaseEnvironment) -> None:
        await self._install_with_mode(environment)

    async def _online_install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=("apt-get update && apt-get install -y --no-install-recommends "
                     "curl ca-certificates"),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(environment, command=self.build_install_command())

    async def _link_from_image(self, environment: BaseEnvironment) -> None:
        # Node 系：build_run_command 以 `. ~/.nvm/nvm.sh` 开头，故把镜像 bin/ 写入该文件的 PATH。
        bindir = f"{self.image_mount}/bin"
        line = f'export PATH="{bindir}:$PATH"'
        await self.exec_as_agent(environment, command=(
            "set -e; mkdir -p ~/.nvm; "
            f"grep -qsF '{line}' ~/.nvm/nvm.sh || echo '{line}' >> ~/.nvm/nvm.sh; "
            f"{NVM_LOAD}; {self._cli_name} --version </dev/null"
        ))

    @property
    def image_mount(self) -> str:
        from lg_bench import harness_images as hi
        return hi.mount_path(self.HARNESS_ID)

    @property
    def _cli_name(self) -> str:
        from lg_bench import harness_images as hi
        return hi.load_lock()["harness"][self.HARNESS_ID].get("cli", self.HARNESS_ID)

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        missing = self.missing_env()
        if missing:
            raise ValueError(f"{self.HARNESS_ID}: missing env {', '.join(missing)}")
        self._instruction = instruction
        for path, content in self.build_config_files().items():
            await self.exec_as_agent(environment, command=self.write_file_command(path, content))
        await self.exec_as_agent(
            environment,
            command=self.build_run_command(shlex.quote(instruction)),
            env=self.build_run_env(),
        )
