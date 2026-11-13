#!/system/bin/sh
set -eu
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
[ "$(id -Z)" = u:r:ksu:s0 ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
# interposer 的两个 bind 先撤，再撤 SVID。
# HAL 可能正映射着 DSO（普通 umount 会 EBUSY），用 lazy umount：挂载从命名空间摘掉，
# 已运行进程保留自己的 inode，重装/卸载都能从干净状态开始。
umount -l /odm/bin/vendor.xiaomi.usb.dp-service 2>/dev/null || umount /odm/bin/vendor.xiaomi.usb.dp-service 2>/dev/null || true
umount -l /odm/lib64/libanc_supermoon.so 2>/dev/null || umount /odm/lib64/libanc_supermoon.so 2>/dev/null || true
umount -l /mnt/vendor/mipad_lib 2>/dev/null || umount /mnt/vendor/mipad_lib 2>/dev/null || true
. "$MODDIR/common.sh"
run_locked uninstall || true
# 卸载必须自己清干净：普通用户没有 adb，不会手动删残留。
# 只有 SVID 确实已不在（或本来就没挂）时才删状态目录，
# 避免把未完成的恢复记录（owner / mount-intent）一起丢掉。
if [ -z "$(entry)" ]; then
 rm -rf "$STATE"
fi
exit 0
