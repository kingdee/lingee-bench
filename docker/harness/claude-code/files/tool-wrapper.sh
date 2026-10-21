#!/bin/sh
# 随 Claude Code 镜像携带的 procps 工具包装：只对这一条命令设置库路径，不影响任务内其他程序。
d=/opt/lg-harness/claude-code/tools
LD_LIBRARY_PATH="$d/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" exec "$d/libexec/$(basename "$0")" "$@"
