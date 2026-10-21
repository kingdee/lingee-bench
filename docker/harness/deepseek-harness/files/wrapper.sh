#!/bin/sh
# lg-bench：只读挂载的 @NAME@ 启动入口。使用镜像自带的 Node 运行时；与在线安装一致，
# CLI 及其子进程的 PATH 中可见该 node。
r=/opt/lg-harness/@NAME@/lib
PATH="$r/node/bin:$PATH" exec "$r/app/node_modules/.bin/@CLI@" "$@"
