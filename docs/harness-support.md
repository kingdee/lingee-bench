# Harness接入与运行

仓库提供Claude Code、Pi、DeepSeek Harness和OpenCode的Harbor适配器。它们使用相同的任务环境与评分器，经每个trial的模型代理访问配置的模型。

## 配置入口

| Harness | Adapter | 单题配置 |
| --- | --- | --- |
| Claude Code | `lg_bench.agents.claude_code:LgClaudeCode` | `configs/jobs/claude-code-glm-5.2-smoke.yaml` |
| Pi | `lg_bench.agents.pi:LgPi` | `configs/jobs/pi-glm-5.2-smoke.yaml` |
| DeepSeek Harness | `lg_bench.agents.deepseek_harness:DeepSeekHarness` | `configs/jobs/deepseek-harness-glm-5.2-smoke.yaml` |
| OpenCode | `lg_bench.agents.opencode:LgOpenCode` | `configs/jobs/opencode-glm-5.2-smoke.yaml` |

Harness版本由`docker/harness/harness-lock.json`和Job的`kwargs.version`固定。安装方式及镜像构建见[Harness镜像](harness-images.md)。

## 模型连接

Job通过`configs/compose/model-proxy.yaml`启动`lg-model-proxy`，Agent容器使用代理地址和占位密钥。真实端点与凭据只注入代理容器。

| 路由 | 环境变量 | 使用者 |
| --- | --- | --- |
| `/anthropic/` | `LG_ANTHROPIC_BASE_URL`、`LG_ANTHROPIC_API_KEY` | Claude Code |
| `/openai/` | `LG_OPENAI_BASE_URL`、`LG_OPENAI_API_KEY` | Pi、DeepSeek Harness、OpenCode |

`LG_PROXY_ALLOWED_MODELS`指定可调用的模型。模型变更时同时更新Job和白名单；模型服务须支持对应的工具调用、流式事件及用量协议。代理请求记录位于trial的`agent/model-proxy/requests.jsonl`。

## 运行

从仓库根目录执行：

```bash
uv sync --locked
docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
uv run lg-bench harness build all
cp .env.example .env.glm
```

在`.env.glm`填入所用模型协议的端点和凭据后，先预检，再运行所选Job：

```bash
uv run lg-bench preflight \
  --config configs/jobs/opencode-glm-5.2-smoke.yaml \
  --env-file .env.glm --require-env --dry-run
uv run harbor run \
  --config configs/jobs/opencode-glm-5.2-smoke.yaml --env-file .env.glm
```

企业管理正式任务的批跑见[企业题集](../datasets/lg-bench-enterprise-v1.0/README.md)，通用任务见[通用任务运行](general-smoke.md)。

## 各Harness的配置

- Claude Code使用Anthropic Messages协议，模型连接与别名由Adapter配置；不要将个人登录态挂载到任务环境。完整说明见[Claude Code手册](claude-code.md)。
- Pi使用`lg-openai`自定义provider，配置写入容器内的`~/.pi/agent/models.json`。
- DeepSeek Harness使用headless profile；`agent/dsh-config.yaml`记录生效配置，事件流保存在`agent/deepseek-harness.jsonl`。
- OpenCode使用`lg-openai`provider。`max_output_tokens`控制单次请求输出上限；会话总时限以任务和Job配置为准。

多步任务须在Job中显式配置`resume_trajectory`，并使用支持原生续聊的Adapter。比较成绩时保留模型、Harness版本、输入、评分版本和预算；执行或评分故障单独统计。

## 容器化运行器

可在容器内运行Harbor：

```bash
docker build -f docker/runner/Dockerfile -t lg-bench/runner:0.1.0 .
LG_BENCH_HOST_DIR=$PWD docker compose -f docker/runner/compose.yaml run --rm runner \
  run --config configs/jobs/pi-glm-5.2-smoke.yaml --job-name <name>
```

运行器挂载Docker socket，需要专用评测环境。仓库挂载路径须与宿主机绝对路径一致，使Docker daemon能够解析trial日志及资源挂载。

## 结果

`jobs/<job>/`保存解析配置、逐题结果、Agent日志、轨迹与verifier输出。用量和成本字段保留各Harness的来源及计数口径；缺失值不能补为零，不同成本估算不能直接当作实际账单比较。
