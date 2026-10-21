# Claude Code运行

Claude Code通过`lg_bench.agents.claude_code:LgClaudeCode`接入Harbor，使用Anthropic Messages协议，经trial模型代理访问配置的模型。单题示例位于`configs/jobs/claude-code-glm-5.2-smoke.yaml`。

## 准备

在仓库根目录执行：

```bash
uv sync --locked
docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
uv run lg-bench harness build claude-code
cp .env.example .env.glm
```

在`.env.glm`填写`LG_ANTHROPIC_BASE_URL`、`LG_ANTHROPIC_API_KEY`及模型白名单。端点须支持Anthropic Messages的工具调用、流式事件和用量字段，密钥须与端点所属服务对应。

Job中的`model_name`须与代理白名单一致。Adapter将容器内模型连接指向`lg-model-proxy`，凭据使用占位值；真实凭据只进入代理。不要将个人Claude配置、OAuth状态或模型密钥挂载到任务环境。

Harness版本与只读安装方式见[Harness镜像](harness-images.md)。

## 检查与运行

```bash
uv run harbor run \
  --config configs/jobs/claude-code-glm-5.2-smoke.yaml --print-config
uv run lg-bench preflight \
  --config configs/jobs/claude-code-glm-5.2-smoke.yaml \
  --env-file .env.glm --require-env --dry-run
uv run harbor run \
  --config configs/jobs/claude-code-glm-5.2-smoke.yaml --env-file .env.glm
```

调整模型、版本或预算时，复制为被忽略的`*.local.yaml`，检查解析后的`agents`、超时与版本。实际运行会调用模型服务，费用以服务账单为准；CLI上报的成本估算保留原始口径。

## 查看结果

`jobs/<job>/`中的`config.json`、`lock.json`记录运行配置与版本，`result.json`记录执行状态和成绩。各trial的`agent/`保存事件流和轨迹，`verifier/`保存评分与诊断，`artifacts/`保存收集到的交付物。

执行异常、模型连接失败和Judge故障单独统计；业务零分依据verifier的正常评分结果判定。安装检查或本地参考解通过不能作为Agent能力成绩。

## 常见问题

| 现象 | 检查 |
| --- | --- |
| Docker连接或镜像失败 | daemon、权限、CPU架构及Harness镜像状态 |
| `AgentSetupTimeoutError` | 构建与安装日志；正式Job使用已构建的只读Harness镜像 |
| 401/403 | 端点、凭据及服务授权；诊断时不输出密钥 |
| 模型不存在或404 | Job模型ID、代理白名单及账号可用模型 |
| 工具调用或流解析异常 | 网关协议与固定CLI版本的兼容性 |
| 轮次、超时或预算耗尽 | 保留原始结果；改变预算后作为另一配置运行 |

企业正式题运行见[企业题集](../datasets/lg-bench-enterprise-v1.0/README.md)，通用题运行见[通用任务运行](general-smoke.md)。
