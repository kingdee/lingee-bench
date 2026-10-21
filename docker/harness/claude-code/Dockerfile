# Claude Code 只读挂载镜像（不发布；用户本地构建，构建时下载即使用上游制品，见上游条款）。
# 由 `lg-bench harness build claude-code` 生成构建上下文后调用，不要直接 docker build。
# 构建上下文：artifacts/（已按锁文件校验的制品 + SHA256SUMS）、debs.sha256、MANIFEST.json、files/。
ARG BUILDER
FROM ${BUILDER} AS build
ARG DEB_PINS
COPY artifacts/ /in/
COPY debs.sha256 /in/debs.sha256
COPY files/ /files/
COPY MANIFEST.json /out/MANIFEST.json
RUN set -eux; \
    cd /in && sha256sum -c SHA256SUMS; \
    mkdir -p /out/bin /out/tools/bin /out/tools/libexec /out/tools/lib /out/LICENSES; \
    install -m 0755 /in/claude /out/bin/claude; \
    # procps：CC 清理进程树需要 ps/pgrep；任务镜像不一定有，按固定版本随镜像携带
    apt-get update -qq; \
    mkdir -p /tmp/deb && cd /tmp/deb && apt-get download ${DEB_PINS}; \
    sha256sum -c /in/debs.sha256; \
    dpkg-deb -x procps_*.deb /tmp/procps; dpkg-deb -x libproc2-0_*.deb /tmp/libproc2; \
    cp /tmp/procps/bin/ps /out/tools/libexec/ps; \
    cp /tmp/procps/usr/bin/pgrep /out/tools/libexec/pgrep; \
    cp /tmp/procps/usr/bin/pgrep /out/tools/libexec/pkill; \
    find /tmp/libproc2 -name 'libproc2.so.0*' -exec cp -a {} /out/tools/lib/ \; ; \
    ls /out/tools/lib/libproc2.so.0 >/dev/null; \
    for t in ps pgrep pkill; do install -m 0755 /files/tool-wrapper.sh /out/tools/bin/$t; done; \
    cp /tmp/procps/usr/share/doc/procps/copyright /out/LICENSES/procps.copyright; \
    cp /tmp/libproc2/usr/share/doc/libproc2-0/copyright /out/LICENSES/libproc2-0.copyright; \
    cp /files/NOTICE /out/LICENSES/NOTICE; \
    rm -rf /var/lib/apt/lists/*

FROM scratch
COPY --from=build /out/ /
