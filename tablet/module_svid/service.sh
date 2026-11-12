#!/system/bin/sh
# 只做一件事：把 adapter_svid 内存 bind-mount 成 0，让厂商 HAL 认为对端是 Mac。
# 不碰 HAL —— 只有系统客户端在插线时自己 prepare 的 HAL 才会既转发给 host
# 又保留本地注入；模块接管 HAL 会让平板自己的触控失效。
set -eu
umask 077
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
[ "$(id -u)" = 0 ] && [ "$(id -Z)" = u:r:ksu:s0 ] || exit 70
[ "$(getenforce)" = Enforcing ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
. "$MODDIR/profile.sh"
profile_check || exit 70
. "$MODDIR/common.sh"
n=0
while [ "$(getprop sys.boot_completed)" != 1 ] && [ "$n" -lt 60 ]; do sleep 1; n=$((n+1)); done
run_locked apply || exit 1
