# ERP MCP接入

正式ERP题使用离线业务服务，经MCP代理访问。业务服务引擎在本仓库，评测实例随题集发布在Hugging Face。每个trial有独立沙箱；代理按题过滤工具，并把请求转发到该题绑定的数据集。

## 组件

| 组件 | 入口 |
| --- | --- |
| MCP代理 | `src/lg_bench/mcp_proxy.py`、`configs/compose/mcp-proxy.yaml` |
| 离线业务服务 | `erp-mock-mcp/`、`configs/compose/erp-mock-mcp.yaml` |
| 评测实例 | 随题集下载到`datasets/environments/erp-mock-mcp/`（见[题集](../datasets/README.md)） |
| 逐题白名单 | `configs/compose/mcp-allow-<dataset>.yaml`（权威来源是题集的`task-bindings.json`，二者须逐题一致） |
| 正式Job | `configs/jobs/erp-cases/` |

## 构建与运行

先下载题集与评测环境，mock镜像构建时打入评测实例：

```bash
docker build -f docker/mcp-proxy/Dockerfile -t lg-bench/mcp-proxy:0.1.0 .
docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
uv run harbor run \
  --config configs/jobs/erp-cases/opencode-glm-5.3-flash-fa-16.yaml \
  --env-file .env.glm
```

模型配置见`.env.example`。离线业务服务不需要真实ERP账号；模型调用仍需要自备端点与凭据。

Job的Compose顺序为模型代理、MCP代理、业务服务、实例、逐题白名单、Judge环境及harness。Agent统一连接`http://lg-mcp-proxy:18090/mcp`，不直接持有业务服务令牌。

## 查询与分页

工具返回的`history`是当前页，`latest`是首条参考。`has_more=true`时保持其他查询参数不变，以`next_offset`继续，直到结果末尾。单页除行数外还有字节限制，不能把请求的`limit`当成实际返回行数。

字段含义、业务日期、组织与统计口径以题面和工具描述为准。无匹配记录、缺失字段和查询错误不能混为同一种业务事实。

## 数据与版本

本仓库的`erp-mock-mcp/packs/`只有人工建模的`core`、`purchase`、`inventory`，供示例实例`p2p-demo`和引擎测试使用。评测实例的业务包与数据集随题集版本发布，下载后位于`datasets/environments/erp-mock-mcp/{packs,instances/erp-benchmark}`。更新题集后校验实例并重建镜像：

```bash
uv run --directory erp-mock-mcp erp-mock-mcp validate \
  --instance ../datasets/environments/erp-mock-mcp/instances/erp-benchmark \
  --packs-dir ../datasets/environments/erp-mock-mcp/packs
uv run python scripts/check_release.py --datasets   # 白名单片段须与题集的 task-bindings.json 逐题一致
```

## 评测实例 erp-benchmark

正式题集的30道任务各绑定实例中的一份数据集，各题只暴露其白名单工具，完整工具面不直接发送给Agent。具体业务包、数据集与版本以下载的`instance.yaml`为准。

`datasets/lg-bench-enterprise-v1.0/dataset.toml`固定正式任务范围。实例中的其他数据集不自动成为正式评测题。每道正式题都有对应白名单片段，缺省数据集为`bgt-05`；正式Job应始终指定本题数据集。

## 接入自备MCP端点

代理也支持把请求转发到自备的MCP端点（`LG_MCP_TARGET=real`），配置写在被忽略的`.env.mcp`中，字段见`.env.example`，并必须设置只读工具白名单。这种模式结果不可复现，不用于正式成绩；端点凭据只进入MCP代理，不进入Agent容器或公开配置。
