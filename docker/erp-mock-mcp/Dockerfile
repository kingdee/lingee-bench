# erp-mock-mcp sidecar 镜像：业务无关、确定性的 ERP mock，经 MCP streamable-http 暴露。
# 构建（仓库根目录，构建上下文含 erp-mock-mcp/ 引擎与下载的评测环境）：
#   docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
# 运行见 configs/compose/erp-mock-mcp.yaml。镜像不含任何真实凭据。
#
# 评测实例 erp-benchmark 及其业务包随题集发布在 Hugging Face，不在本仓库：先按
# datasets/README.md 下载到 datasets/environments/erp-mock-mcp/，再构建本镜像。
# 放在别处时用 --build-arg ERP_MOCK_ENV=<相对构建上下文的目录> 指定。目录不存在时构建直接失败，
# 不会产出缺实例的镜像。
# 镜像只内置评测实例 erp-benchmark；示例实例 instances/p2p-demo 是 demo 数据，
# 不进交付镜像（见下面的 COPY 与 CMD）。
# 以 --host 0.0.0.0 对 trial 网络监听时，erp-mock 要求设置 ERP_MOCK_MCP_TOKEN（compose 注入），
# 否则按设计拒绝启动（退出码 2）。Admin 端口（8001）始终只在回环地址，不对外暴露。
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12.14-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/erp-mock-mcp/.venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    PATH=/opt/erp-mock-mcp/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /opt/erp-mock-mcp
# 先装锁定依赖（含项目自身），业务配置目录后拷入，避免改配置就失效依赖层缓存。
# 构建 wheel 只需 src/erp_mock（pyproject 的 hatch 配置），所以此处无需 packs/instances。
COPY erp-mock-mcp/pyproject.toml erp-mock-mcp/uv.lock ./
COPY erp-mock-mcp/src ./src
RUN uv sync --locked --no-dev --no-editable && rm -rf /root/.cache/uv
# 运行期按路径读取的业务配置（只读）。先拷仓库自带的人工包（core 等），再叠加评测环境的业务包：
# 同名包以评测环境为准，保证与 instance.yaml 固定的版本一致。
ARG ERP_MOCK_ENV=datasets/environments/erp-mock-mcp
COPY erp-mock-mcp/packs ./packs
COPY ${ERP_MOCK_ENV}/packs ./packs
# 实例配置按目录逐个拷入，不整目录拷 instances/：示例实例 instances/p2p-demo 是 demo 数据，
# 不能进对外交付的镜像。它仍留在仓库里，供本地开发、契约测试与业务包教程使用。
# 新增评测实例要在此显式加一行，漏加的后果是 serve 加载校验失败并退出，不会静默服务错实例。
COPY ${ERP_MOCK_ENV}/instances/erp-benchmark ./instances/erp-benchmark
# 运行期数据目录（快照缓存与沙箱库）；非 root 用户可写。
RUN useradd --system --uid 10003 erpmock \
 && mkdir -p /var/lib/erp-mock \
 && chown -R erpmock:erpmock /var/lib/erp-mock
USER erpmock
EXPOSE 8000
# 就绪探针：serve 先做全部加载校验再绑定端口，MCP 端口可建连即表示实例配置已就绪。
HEALTHCHECK --interval=1s --timeout=3s --retries=60 \
  CMD python -c "import socket,sys; socket.create_connection(('127.0.0.1',8000),timeout=2); sys.exit(0)"
ENTRYPOINT ["erp-mock-mcp"]
# 默认实例是评测实例，不是示例实例：忘记挂 configs/compose/erp-mock-benchmark.yaml 时
# 服务的仍是 erp-benchmark，不会静默回落到 demo 数据。
CMD ["serve", "--instance", "/opt/erp-mock-mcp/instances/erp-benchmark", \
     "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/var/lib/erp-mock"]
