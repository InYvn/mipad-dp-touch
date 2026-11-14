#!/system/bin/sh
# KernelSU 卸载模块前调用：只停守护进程，不删任何用户数据（state 目录保留供事后分析）。
set -u
case $0 in
*/*) MODDIR=${0%/*} ;;
*) MODDIR=. ;;
esac
if [ -r "$MODDIR/bin/dpin-guard.sh" ]; then
	sh "$MODDIR/bin/dpin-guard.sh" stop >/dev/null 2>&1 || true
fi
exit 0
