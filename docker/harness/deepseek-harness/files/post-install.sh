#!/bin/sh
# DeepSeek Harness：依赖树中五个安装脚本已审阅：
# - node-pty install（prebuild.js || node-gyp rebuild）：prebuilds/linux-{arm64,x64} 已随包发布，不需编译；
# - node-pty postinstall：清理 build/Release，与预编译无关；
# - koffi install（cnoke --prebuild）：预编译库随包发布于 lib/native；
# - protobufjs postinstall：仅版本提示；@google/genai preinstall：no-op；
# - @deepseek-ai/dsh-subprocess-local postinstall：恢复 node-pty spawn-helper 的可执行位——
#   这是唯一可能需要的步骤，在此显式执行（不联网，只 chmod）。
set -eu
node node_modules/@deepseek-ai/dsh-subprocess-local/scripts/ensure-spawn-helper.mjs
