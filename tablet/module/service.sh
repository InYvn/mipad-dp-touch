#!/system/bin/sh
# KernelSU profile only. Native supervisor owns watchdog + persistent prepare chain.
set -eu
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
S=/data/adb/mipad_fullsplit_local
[ ! -L "$S" ] || exit 70
mkdir -p "$S"; chmod 700 "$S"
# Native flock serializes initial staging against all live controller generations.
"$MODDIR/module-controller" once "$MODDIR" "$S" "$MODDIR/bootstrap.sh" || exit $?
# Persistent monitor restarts the supervisor/watchdog and retains prepare ownership
# after disable/remove until independently verified system-client takeover.
exec "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S"
