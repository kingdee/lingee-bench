# lg-bench 模型代理 sidecar 镜像。构建（在仓库根目录）：
#   docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
# 只用 Python 标准库；不含任何凭据，凭据在 trial 启动时由 compose 注入。
FROM python:3.12.14-slim-bookworm
COPY src/lg_bench/model_proxy.py /opt/lg-model-proxy/model_proxy.py
RUN useradd --system --uid 10001 lgproxy
USER lgproxy
ENV LG_PROXY_PORT=18080 PYTHONUNBUFFERED=1
EXPOSE 18080
HEALTHCHECK --interval=1s --timeout=3s --retries=30 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/health',timeout=2).status==200 else 1)"
ENTRYPOINT ["python", "/opt/lg-model-proxy/model_proxy.py"]
