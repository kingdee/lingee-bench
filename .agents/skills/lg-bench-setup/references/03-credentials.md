# 阶段 3：凭据

目标：`.env.glm` 定义了本次 Agent 和 Judge 需要的变量，模型白名单放行本次要用的模型。这些值只经 compose 插值进入模型代理 sidecar，agent 容器里只有代理地址和占位密钥。

**先读 `.env.example`**，它是带注释的权威模板。

## 1. 按用途选变量

| 用途 | 协议（代理路由） | 需要的变量 |
|---|---|---|
| Claude Code 的 Agent | Anthropic Messages（`/anthropic`） | `LG_ANTHROPIC_BASE_URL`、`LG_ANTHROPIC_API_KEY` |
| Pi、DeepSeek Harness、OpenCode 的 Agent | OpenAI Chat Completions（`/openai`） | `LG_OPENAI_BASE_URL`、`LG_OPENAI_API_KEY` |
| 通用题和 ERP 题的 Judge | OpenAI Chat Completions（`/openai`） | `LG_OPENAI_BASE_URL`、`LG_OPENAI_API_KEY` |

注意：只要跑通用题或 ERP 题，即使 harness 是 Claude Code，也必须填 `LG_OPENAI_*` 给 Judge 用。preflight 只检查 Agent 需要的变量，不检查 Judge，这一点要自己核对。示例题冒烟没有 Judge。

## 2. 填写

1. 文件不存在时创建：
   ```bash
   test -f .env.glm || cp .env.example .env.glm
   ```
2. 请用户**自己编辑** `.env.glm` 填值，不要让用户把密钥贴进对话。
3. 用预检确认（只输出 set/missing，不输出值）：
   ```bash
   uv run lg-bench preflight --config <job.yaml> --env-file .env.glm --require-env
   ```

地址怎么填：

- `LG_ANTHROPIC_BASE_URL` 必须是 Anthropic 兼容端点，例如百炼 `https://dashscope.aliyuncs.com/apps/anthropic`、智谱 `https://open.bigmodel.cn/api/anthropic`、Z.AI `https://api.z.ai/api/anthropic`。**不能**填 OpenAI 兼容地址（如百炼 `compatible-mode/v1`）。
- `LG_OPENAI_BASE_URL` 填到 `/v1` 这一级，不含 `/chat/completions`，例如 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- 密钥必须与端点所属平台对应。账号是否有所选模型的权限要用户自己确认。

## 3. 模型白名单

代理只放行白名单里的模型，其他模型请求一律返回 400。白名单要同时包含 Agent 模型和 Judge 模型：

| 场景 | 白名单来源 |
|---|---|
| 示例题冒烟、ERP 题、ERP 批跑 | `.env.glm` 的 `LG_PROXY_ALLOWED_MODELS`（逗号分隔，不设时默认只有 `glm-5.2`） |
| 通用题正式 job `opencode-glm-5.3-flash-general-smoke.yaml` | 由 `configs/compose/model-glm-5.3-flash.yaml` 固定为 `glm-5.3-flash` |
| 用 `general_job.py` 生成的通用批跑 job | 由生成的 `configs/compose/model-<名字>.local.yaml` 固定为本次的 Agent 和 Judge 模型 |

例如 ERP 批跑用 `glm-5.3-flash` 做 Agent、`glm-5.3` 做 Judge，就在 `.env.glm` 里设 `LG_PROXY_ALLOWED_MODELS=glm-5.3-flash,glm-5.3`。

## 护栏

- env 文件里**不要**设置 `ANTHROPIC_*` / `OPENAI_*`，只用 `LG_` 前缀变量。终端里已有 Anthropic key、OAuth、Bedrock 配置时，换一个干净终端。
- 离线 ERP mock 不需要任何 ERP 凭据。接自备 MCP 端点（real 模式，不用于正式成绩）时才用单独的 `.env.mcp`，详见 `docs/mcp-integration.md`。
