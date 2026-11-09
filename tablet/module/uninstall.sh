#!/system/bin/sh
set -eu
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
touch "$MODDIR/remove"
S=/data/adb/mipad_fullsplit_local
[ -f "$S/runtime-ready" ] || exit 0
[ "$(id -Z)" = u:r:ksu:s0 ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
# Persistent state deliberately survives removal: original HAL may own our client.
n=0
while [ "$n" -lt 60 ]; do
 "$S/module-controller" run "$MODDIR" "$S" "$S/platform.sh" && rc=0 || rc=$?
 if [ "$rc" != 73 ]; then
  if [ -e "$S/pending" ] || [ -e "$S/recovery-owner" ]; then
   "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S" </dev/null >> "$S/removal-monitor.log" 2>&1 &
  fi
  exit "$rc"
 fi
 sleep 1; n=$((n+1))
done
exit 75
