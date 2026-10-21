#!/bin/sh
# Pi：上游推荐 `npm install --ignore-scripts`。依赖树中的三个安装脚本已审阅，均无需执行：
# - esbuild postinstall：校验平台二进制；平台包 @esbuild/linux-* 已作为 optionalDependency 安装；
# - protobufjs postinstall：仅版本方案提示；
# - @google/genai preinstall：no-op。
set -eu
