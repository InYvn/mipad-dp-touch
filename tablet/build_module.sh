#!/bin/sh
# 将已校验的模块载荷确定性打包；不重新编译 native，不连接或修改设备。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
exec python3 "$HERE/package_module.py" "$@"
