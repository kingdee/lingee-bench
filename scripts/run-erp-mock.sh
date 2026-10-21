#!/usr/bin/env bash
# 离线 mock 连通性批次：4 个 harness 顺序经 MCP 代理连 erp-mock sidecar。
# 见 docs/mcp-integration.md 第 7 节。mock 模式不需要 .env.mcp。
#
# 前置：
#   - 模型凭据：.env.glm
#   - ERP 评测实例：datasets/environments/erp-mock-mcp（见 datasets/README.md），镜像构建时打入
#   - 镜像：lg-bench/mcp-proxy:0.1.0 + lg-bench/erp-mock-mcp:0.1.0（脚本会构建，除非 LG_SKIP_BUILD=1）
#
# 用法：
#   ./scripts/run-erp-mock.sh                 # 跑全部 4 个
#   ./scripts/run-erp-mock.sh pi              # 只跑文件名含 pi 的
#   LG_SKIP_BUILD=1 ./scripts/run-erp-mock.sh # 跳过镜像构建
set -euo pipefail
cd "$(dirname "$0")/.."

filter="${1:-}"
# 强制 mock，并清掉可能从 shell 残留的 real 配置，防止 mock 批次被悄悄路由到真实网关。
export LG_MCP_TARGET=mock
unset LG_ERP_MCP_URL LG_ERP_MCP_HEADERS LG_MCP_ALLOWED_TOOLS || true

if [ "${LG_SKIP_BUILD:-0}" != "1" ]; then
  docker build -f docker/mcp-proxy/Dockerfile    -t lg-bench/mcp-proxy:0.1.0 .
  docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
fi

ran=0
for cfg in configs/jobs/erp-mcp-mock-smoke/*.yaml; do
  [ -n "$filter" ] && [[ "$cfg" != *"$filter"* ]] && continue
  menv=.env.glm
  [ -f "$menv" ] || { echo "跳过 $(basename "$cfg")：缺少模型凭据 $menv" >&2; continue; }
  name="erp-mock-$(basename "$cfg" .yaml)"
  echo ">>> [mock] $(basename "$cfg")  model=$menv  job=$name"
  uv run harbor run --config "$cfg" --env-file "$menv" --job-name "$name"
  ran=$((ran + 1))
done

echo "完成，共跑 $ran 个 job。确认实际上游（应为 erp-mock:8000）："
echo "  grep -rho '\"upstream_host\":[^,]*' jobs/erp-mock-* | sort -u"
