"""DeepSeek Harness（dsh，https://github.com/deepseek-ai/deepseek-harness）的 Harbor adapter。

上游依据：
- 安装：npm 包 ``@deepseek-ai/dsh``（bin ``dsh``），README 的 Run from npm
- 非交互：``dsh --profile headless --json "<task>"``，见
  packages/bundle/headless/README.md：退出码 0=完成、1=中止/出错；``--json`` 输出
  session/status/text/thinking/tool_call/tool_result/final/error 的 NDJSON 事件流
- 模型：docs/user/guide/providers.md，``llm-pi-ai`` 自定义 provider（``apiKeyEnv``、
  ``api: openai-completions``、``baseURL``、``compat``），写入
  ``$DSH_HOME/profiles/<profile>/cordis.patch.yml``；默认模型由 ``agent-default-model``
  插件决定（社区讨论 deepseek-ai/deepseek-harness#4326 验证过 headless 覆盖方式）

状态：已完成 GLM-5.2 单题冒烟（2026-10-12，reward=1）。dsh 处于 developer preview，
配置字段可能随版本变化；adapter 在运行前把 ``--dump-config`` 结果存入日志，核对 provider/模型。
token 用量取自 ``status`` 事件 ``phase=="step_end"`` 的 ``usage``（见
``populate_context_post_run``）；0.2.0-rc.2 的 ``final`` 事件不带 usage。
Harbor 原生 resume 使用首轮 session 事件的精确 ID 调用 ``--session-id``。
每轮单独导出 ATIF，终端回复取无截断的 ``final.text``，用户题面取实际传入的本轮指令。
"""

from __future__ import annotations

import json
import shlex
from typing import override

from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.observation import Observation, ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory

from lg_bench.agents._base import (AGENT_LOG_DIR, API_KEY_ENV, NVM_LOAD,
                                   LgInstalledAgent)
from lg_bench.agents._mcp import _check_name

DSH_HOME = "$HOME/.dsh"
PROFILE = "headless"


def _yaml_str(value: str) -> str:
    # JSON 字符串是合法的 YAML 双引号标量。
    return json.dumps(value)


class DeepSeekHarness(LgInstalledAgent):
    SUPPORTS_RESUME = True
    HARNESS_ID = "deepseek-harness"
    PINNED_VERSION = "0.2.0-rc.2"
    OUTPUT_FILENAME = "deepseek-harness.jsonl"
    PROVIDER_ID = "lg-openai"

    def __init__(self, logs_dir, *args, supports_developer_role: bool = False,
                 max_tokens_field: str = "max_tokens", max_tokens: int = 65535, **kwargs):
        self._supports_developer_role = bool(supports_developer_role)
        self._max_tokens_field = max_tokens_field
        # 显式钉住每请求输出上限（含推理预算），与 opencode 侧 maxOutputTokens 统一口径。
        self._max_tokens = max_tokens
        self._session_id: str | None = None
        super().__init__(logs_dir, *args, **kwargs)
        if self._max_turns is not None:
            raise ValueError("deepseek-harness: headless mode has no max_turns option; "
                             "rely on the task timeout")

    @staticmethod
    @override
    def name() -> str:
        return "deepseek-harness"

    @override
    def get_version_command(self) -> str | None:
        # image 与 online 两种模式下 dsh 都在 PATH 上，`dsh --version` 均可用。
        return f"{NVM_LOAD}; dsh --version </dev/null"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    @override
    def build_install_command(self) -> str:
        return ("set -euo pipefail; "
                f"{self.node_install_snippet()} && "
                f"npm install -g @deepseek-ai/dsh@{self._version} && "
                "command -v dsh")

    @override
    def harness_env(self) -> dict[str, str]:
        # DSH_HOME 含 $HOME，exec env 不做 shell 展开，因此在命令里 export。
        return {"DSH_TELEMETRY_DISABLED": "1"}

    @override
    def build_config_files(self, *, placeholder: bool = False) -> dict[str, str]:
        dev_role = "true" if self._supports_developer_role else "false"
        patch = "\n".join([
            "# lg-bench 生成：把 headless profile 固定到同一 OpenAI 兼容模型服务。",
            "- id: llm-pi-ai",
            "  config:",
            "    providers:",
            f"      {self.PROVIDER_ID}:",
            f"        apiKeyEnv: {API_KEY_ENV}",
            "        api: openai-completions",
            f"        baseURL: {_yaml_str(self.base_url(placeholder=placeholder))}",
            "        compat:",
            f"          supportsDeveloperRole: {dev_role}",
            f"          maxTokensField: {self._max_tokens_field}",
            "        models:",
            f"          - id: {_yaml_str(self.model_name)}",
            f"            maxTokens: {self._max_tokens}",
            "- id: agent-default-model",
            "  config:",
            f"    provider: {self.PROVIDER_ID}",
            f"    model: {_yaml_str(self.model_name)}",
            *self._mcp_patch_lines(),
            "",
        ])
        return {f"{DSH_HOME}/profiles/{PROFILE}/cordis.patch.yml": patch}

    def _mcp_patch_lines(self) -> list[str]:
        """每个 MCP server 一个 ``@deepseek-ai/dsh-mcp-client`` 插件实例（镜像已内置该包）。

        工具名为 ``mcp__<server>__<tool>``，与 Claude Code / Pi 一致。默认不启用任何 server，
        必须显式插入；连接失败不阻断启动（``failOnStartupError`` 默认 false）。
        """
        if not self.mcp_servers:
            return []
        lines = ["- insert:"]
        for server in self.mcp_servers:
            _check_name(server.name, self.HARNESS_ID)
            lines += [f"    - id: {_yaml_str('mcp-' + server.name)}",
                      "      name: '@deepseek-ai/dsh-mcp-client'",
                      "      config:",
                      f"        serverName: {_yaml_str(server.name)}"]
            if server.transport == "stdio":
                lines += ["        transport: stdio",
                          f"        command: {_yaml_str(server.command)}",
                          f"        args: {json.dumps(list(server.args), ensure_ascii=False)}"]
            elif server.transport == "streamable-http":
                lines += ["        transport: streamable-http",
                          f"        url: {_yaml_str(server.url)}"]
            else:
                raise ValueError(f"{self.HARNESS_ID}: MCP server {server.name!r}: "
                                 "sse transport is not supported; use streamable-http")
        return lines

    @override
    def build_run_command(self, instruction: str) -> str:
        resume = ""
        if self._resume:
            if not self._session_id:
                raise ValueError("deepseek-harness: no collected session id to resume")
            resume = f"--session-id {shlex.quote(self._session_id)} "
        prefix = f'{NVM_LOAD}; export DSH_HOME="{DSH_HOME}";'
        # 先导出组合后的配置（不调用模型），便于核对 provider/模型是否生效。
        dump = (f"dsh --profile {PROFILE} --dump-config "
                f"> {AGENT_LOG_DIR}/dsh-config.yaml 2>&1 || true;")
        run = (f"dsh --profile {PROFILE} --json {resume}-- {instruction} </dev/null"
               f" 2>{AGENT_LOG_DIR}/deepseek-harness.stderr"
               f" | stdbuf -oL tee {AGENT_LOG_DIR}/{self.OUTPUT_FILENAME}")
        return f"{prefix} {dump} {run}"

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        # 实测 0.2.0-rc.2 + GLM-5.2（2026-10-12）：headless --json 事件流中，每个
        # ``status`` 事件在 ``phase=="step_end"`` 时带 ``usage``，形如
        # {"inputTokens": 538, "outputTokens": 75, "totalTokens": 6117, "cacheReadTokens": 5504}
        # （单步值，totalTokens == inputTokens + outputTokens + cacheReadTokens；inputTokens 为
        # 非缓存输入）。``final`` 事件只有文本、无 usage，故按 step_end 逐步累加。
        path = self.logs_dir / self.OUTPUT_FILENAME
        if not path.exists():
            return
        counts: dict[str, int] = {}
        session_id = None
        has_final = False
        error = None
        n_input = n_cache = n_output = 0
        n_steps = 0
        events = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            kind = str(event.get("type"))
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "session":
                session_id = event.get("sessionId") or event.get("id")
            elif kind == "final":
                has_final = True
            elif kind == "error":
                error = event.get("message") or event.get("code")
            elif kind == "status" and event.get("phase") == "step_end":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    n_steps += 1
                    cache = int(usage.get("cacheReadTokens") or 0)
                    # n_input_tokens 代表含缓存的整段输入；inputTokens 为非缓存部分。
                    n_input += int(usage.get("inputTokens") or 0) + cache
                    n_cache += cache
                    n_output += int(usage.get("outputTokens") or 0)
        self._session_id = session_id
        context.metadata = {**(context.metadata or {}), "harness": self.HARNESS_ID,
                            "dsh_session_id": session_id, "event_counts": counts,
                            "has_final": has_final, "error": error, "n_steps": n_steps}
        if n_steps:
            context.n_input_tokens = n_input
            context.n_cache_tokens = n_cache
            context.n_output_tokens = n_output
        if session_id and has_final:
            self._write_trajectory(events, context)

    def _write_trajectory(self, events: list[dict], context: AgentContext) -> None:
        """Project this invocation's native stream, retaining its terminal final verbatim."""
        steps = [Step(step_id=1, source="user", message=self._instruction)]
        text, thinking = [], []
        calls = {}
        for event in events:
            kind = event.get("type")
            if kind == "text":
                text.append(event["text"])
            elif kind == "thinking":
                thinking.append(event["text"])
            elif kind in {"tool_call", "final"}:
                step = Step(
                    step_id=len(steps) + 1, source="agent", model_name=self.model_name,
                    message=event["text"] if kind == "final" else "\n".join(text),
                    reasoning_content="\n".join(thinking) or None,
                )
                if kind == "tool_call":
                    arguments = event.get("input", {})
                    step.tool_calls = [ToolCall(
                        tool_call_id=event["callId"], function_name=event["tool"],
                        arguments=arguments if isinstance(arguments, dict) else {"value": arguments},
                    )]
                    if event.get("truncated"):
                        step.extra = {"native_payload_truncated": True}
                    calls[event["callId"]] = step
                steps.append(step)
                text, thinking = [], []
            elif kind == "tool_result" and event.get("callId") in calls:
                step = calls[event["callId"]]
                if step.observation is None:
                    step.observation = Observation(results=[])
                step.observation.results.append(ObservationResult(
                    source_call_id=event["callId"], content=event.get("result", ""),
                ))
        trajectory = Trajectory(
            session_id=self._session_id,
            agent=Agent(name=self.name(), version=self.version(), model_name=self.model_name),
            steps=steps,
            final_metrics=FinalMetrics(
                total_prompt_tokens=context.n_input_tokens,
                total_completion_tokens=context.n_output_tokens,
                total_cached_tokens=context.n_cache_tokens,
                total_steps=len(steps),
            ),
        )
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps(trajectory.to_json_dict(), ensure_ascii=False, indent=2) + "\n"
        )
