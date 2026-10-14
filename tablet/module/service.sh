#!/system/bin/sh
# 开机后期再补一次（service 阶段）—— 万一 sysfs 节点是晚于 post-fs-data 才出现的。
# apply 是幂等的：已经挂着就直接跳过。
MODDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)   # 手跑也能用（$0 可能没斜杠）
. "$MODDIR/common.sh"
apply
