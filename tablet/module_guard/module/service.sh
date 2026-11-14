#!/system/bin/sh
# KernelSU late_start service 阶段（root, ksu 域）。启动 DP-in 回调守卫。
# 注意：本模块不修改 /odm、不 reload 触摸模块；与触摸模块 mipad_dptouch_fullsplit_local 互不依赖。
set -u
umask 077
case $0 in
*/*) MODDIR=${0%/*} ;;
*) MODDIR=. ;;
esac
BIN="$MODDIR/bin"
. "$BIN/dpin-lib.sh"
dpin_load_config
dpin_state_init
if [ "$ENABLE" != "1" ]; then
	dpin_log "guard 未启用（ENABLE=$ENABLE），service.sh 不启动"
	exit 0
fi
dpin_boot_report
sh "$BIN/dpin-guard.sh" start
exit 0
