#!/system/bin/sh
# KernelSU post-fs-data 阶段（/data 已挂载，属性服务已可用）。
# 默认什么都不做：EARLY_START=0。设为 1 时只在 HAL 缺失的情况下补一次 ctl.start（实验项，见 FIX-NOTES §5）。
set -u
umask 077
case $0 in
*/*) MODDIR=${0%/*} ;;
*) MODDIR=. ;;
esac
. "$MODDIR/bin/dpin-lib.sh"
dpin_load_config
[ "$EARLY_START" = "1" ] || exit 0
dpin_state_init
dpin_ensure_hal "post-fs-data" 0 || true
exit 0
