#!/system/bin/sh
# KernelSU profile only. Native supervisor owns watchdog + persistent prepare chain.
set -eu
# Init hands module services umask 000, so every shell redirect would create
# 0666 state records; platform-tool's strict reader (q_record) rejects any
# group/other-writable record and the wait path then returns 1 instead of 78,
# which makes runtime-monitor write `disable` after three failures.
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
S=/data/adb/mipad_fullsplit_local
[ ! -L "$S" ] || exit 70
mkdir -p "$S"; chmod 700 "$S"
# --- state-record mode repair: begin ---
# umask 077 only affects newly created files; `>` truncates an existing file and
# keeps its old mode, so records written by earlier generations stay 0666 and the
# strict reader keeps rejecting them. Repair them once before the monitor starts.
for f in hal wait-hal baseline-owner current-owner descriptor-length0 descriptor-length1; do
 if [ -f "$S/$f" ] && [ ! -L "$S/$f" ]; then chmod 600 "$S/$f"; fi
done
# --- state-record mode repair: end ---
# Native flock serializes initial staging against all live controller generations.
"$MODDIR/module-controller" once "$MODDIR" "$S" "$MODDIR/bootstrap.sh" || exit $?
# Persistent monitor restarts the supervisor/watchdog and retains prepare ownership
# after disable/remove until independently verified system-client takeover.
exec "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S"
