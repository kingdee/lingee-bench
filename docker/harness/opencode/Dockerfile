# OpenCode 只读挂载镜像。由 `lg-bench harness build opencode` 生成构建上下文后调用。
# 构建上下文：artifacts/（平台包 tgz + SHA512SUMS，取自 npm integrity）、MANIFEST.json、files/。
ARG BUILDER
FROM ${BUILDER} AS build
COPY artifacts/ /in/
COPY files/ /files/
COPY MANIFEST.json /out/MANIFEST.json
RUN set -eux; \
    cd /in && sha512sum -c SHA512SUMS; \
    mkdir -p /out/bin /out/LICENSES /tmp/pkg; \
    tar xzf /in/opencode-*.tgz -C /tmp/pkg; \
    install -m 0755 /tmp/pkg/package/bin/opencode /out/bin/opencode; \
    cp /tmp/pkg/package/package.json /out/LICENSES/opencode-platform-package.json; \
    cp /files/NOTICE /out/LICENSES/NOTICE

FROM scratch
COPY --from=build /out/ /
