"""项目自建的 Harbor harness adapter。

job YAML 通过 ``import_path`` 引用：

- ``lg_bench.agents.pi:LgPi``
- ``lg_bench.agents.deepseek_harness:DeepSeekHarness``
- ``lg_bench.agents.opencode:LgOpenCode``（Harbor 内置 opencode 的薄子类）
- ``lg_bench.agents.claude_code:LgClaudeCode``（Harbor 内置 claude-code 的薄子类）

四个必选 harness 都只连接每个 trial 的模型代理 sidecar（``configs/compose/model-proxy.yaml``）：
Claude Code 走 ``/anthropic`` 路由，其余走 ``/openai`` 路由；真实端点与密钥只在代理容器中。
状态见 docs/harness-support.md。
"""
