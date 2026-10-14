#!/system/bin/sh
# 开机早期应用（post-fs-data 阶段）—— 尽量赶在 USB/DP 服务起来之前挂好。
MODDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)   # 手跑也能用（$0 可能没斜杠）
. "$MODDIR/common.sh"
apply
