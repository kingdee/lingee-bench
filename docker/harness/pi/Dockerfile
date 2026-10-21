# Node 系 harness 只读挂载镜像（模板，三个 harness 目录各有一份相同副本，参与各自的 lockhash）。
# 由 `lg-bench harness build <name>` 生成构建上下文后调用，不要直接 docker build。
# - Node 运行时取自锁定 digest 的 node 镜像，并按锁文件校验 node 二进制 sha256；
# - 依赖按 package-lock.json 用 `npm ci --ignore-scripts` 安装（integrity 由 npm 校验），
#   确需的安装后步骤只在 files/post-install.sh 中显式执行；
# - 生成 lib/ 的文件树哈希 TREE.sha256，安装时在容器内按锁文件复核。
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS build
ARG NAME
ARG CLI
ARG NODE_SHA256
COPY package.json package-lock.json /out/lib/app/
COPY files/ /files/
COPY MANIFEST.json /out/MANIFEST.json
RUN set -eux; \
    echo "${NODE_SHA256}  /usr/local/bin/node" | sha256sum -c -; \
    mkdir -p /out/lib/node/bin /out/bin /out/LICENSES; \
    cp /usr/local/bin/node /out/lib/node/bin/node; \
    cd /out/lib/app; \
    npm ci --ignore-scripts --omit=dev --no-audit --no-fund --no-update-notifier; \
    sh /files/post-install.sh; \
    rm -rf /root/.npm /tmp/*; \
    sed -e "s|@NAME@|${NAME}|g" -e "s|@CLI@|${CLI}|g" /files/wrapper.sh > /out/bin/${CLI}; \
    chmod 0755 /out/bin/${CLI}; \
    node /files/license-report.cjs /out/lib/app/node_modules > /out/LICENSES/packages.json; \
    cp /files/NOTICE /out/LICENSES/NOTICE; \
    cd /out/lib; \
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /out/TREE.sha256list; \
    sha256sum < /out/TREE.sha256list | cut -d' ' -f1 > /out/TREE.sha256

FROM scratch
COPY --from=build /out/ /
