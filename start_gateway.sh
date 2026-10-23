#!/usr/bin/env bash
# 启动网关（虚拟机里用）
#
#   ./start_gateway.sh                         # 8000 端口，本地模拟上游
#   ./start_gateway.sh --port 8000 --quota 100 # 指定端口和额度
#   ./start_gateway.sh --tokens "A=现场令牌A,B=现场令牌B"
#   ./start_gateway.sh --upstream https://上游/v1/chat/completions --upstream-key "$KEY"
#
# 参赛程序与网关都在本机时用 127.0.0.1；要让别的机器连进来用 0.0.0.0（默认）。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m rmtc_gateway "$@"
