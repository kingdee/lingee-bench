# Harness预构建镜像

正式Job使用预构建镜像，以只读卷提供Harness可执行文件。版本、下载制品及校验值登记在`docker/harness/harness-lock.json`，各Harness的Dockerfile和通知文件位于`docker/harness/`。

## 构建与检查

在仓库根目录执行：

```bash
uv run lg-bench harness build all
uv run lg-bench harness status
```

也可只构建一种Harness：

```bash
uv run lg-bench harness build opencode
```

构建需要Docker及制品下载网络。已有制品可通过`uv run lg-bench harness seed <file>`导入本机缓存；工具按锁文件校验哈希，不接受未登记的制品。

构建缓存位于`.cache/lg-harness/`。构建完成后生成或更新`configs/compose/harness/<name>.yaml`，正式Job在`environment.extra_docker_compose`中引用该片段。

## 锁定与校验

镜像标签为`lg-bench/harness-<name>:<version>-<lockhash12>`。完整lockhash由锁条目、基础镜像和构建上下文计算，写入镜像label、`MANIFEST.json`及本机构建记录。

安装时核对完整lockhash、架构、二进制或文件树校验值、glibc与版本。修改锁文件或构建输入后须重新构建，不能复用旧标签下的内容。

镜像挂载使用Docker image volume，要求Docker Engine API支持该卷类型。镜像根目录只读挂到`/opt/lg-harness/<name>`；任务容器通过Adapter配置的入口调用CLI。平台和原生模块须与任务容器匹配。

## 安装模式

| `install_mode` | 行为 |
| --- | --- |
| `image` | 正式Job使用；校验挂载和版本，失败即停止 |
| `auto` | 挂载存在时使用镜像，否则在线安装；记录实际模式 |
| `online` | 在线安装固定版本，用于调试；成绩报告须注明安装方式 |

构建后的镜像属于本机资源，源码仓库不包含第三方Harness二进制。相关作者、版权和许可声明见各构建目录的`files/NOTICE`；镜像分发须遵守对应组件的条款。

## 运行记录

trial的`agent/harness-provenance.json`保存Harness版本、lockhash、架构及安装方式。本机构建记录保存镜像ID；跨机器复现时依据锁定输入和校验值核对。

```bash
uv run lg-bench preflight \
  --config configs/jobs/opencode-glm-5.2-smoke.yaml \
  --env-file .env.glm --require-env --dry-run
```

预检会检查Job、Compose片段与本机镜像。CI可用`--skip-local-images`做配置检查；实际运行前仍须检查运行机器的镜像。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 缺少镜像或完整lockhash不一致 | 按当前锁文件重新构建，并使用对应Compose片段 |
| 架构、glibc或原生模块不匹配 | 核对任务基础镜像、构建平台及锁文件中的平台校验值 |
| 制品下载或校验失败 | 核对下载源、网络与锁文件，不跳过哈希检查 |
| `image`模式安装失败 | 检查挂载路径和校验日志；修复构建或配置后再运行 |

模型连接、运行预算及结果查看见[Harness接入](harness-support.md)。
