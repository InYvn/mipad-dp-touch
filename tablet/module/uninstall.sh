#!/system/bin/sh
# 卸载模块：把 bind mount 摘掉、清掉状态目录。系统文件从头到尾没被改过，所以这里没什么要还原的。
MODDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)   # 手跑也能用（$0 可能没斜杠）
. "$MODDIR/common.sh"
remove
rm -rf "$STATE"
