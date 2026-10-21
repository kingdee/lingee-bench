# lg-bench 评测运行器镜像（部署方式 1：单机 docker compose，复用宿主机 Docker daemon）。
# 构建（仓库根目录）：docker build -f docker/runner/Dockerfile -t lg-bench/runner:0.1.0 .
# 运行方式见 docker/runner/compose.yaml；镜像内不含任何凭据。
FROM docker:29-cli AS dockercli
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12.14-slim-bookworm
# Harbor 通过 docker CLI + compose 插件管理 trial 容器
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose \
     /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/lg-bench/.venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    PATH=/opt/lg-bench/.venv/bin:$PATH
WORKDIR /opt/lg-bench
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && rm -rf /root/.cache/uv
# 工作目录在运行时挂载为与宿主机相同的绝对路径（Harbor 的 bind mount 由宿主机 daemon 解析）
ENTRYPOINT ["harbor"]
