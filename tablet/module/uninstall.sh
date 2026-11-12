#!/system/bin/sh
set -eu
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
S=/data/adb/mipad_fullsplit_local
[ -f "$S/runtime-ready" ] || exit 0
[ "$(id -Z)" = u:r:ksu:s0 ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
# Persistent state deliberately survives removal: original HAL may own our client.
# 2.1.2 F4: publish the removal intent first so a concurrently running monitor
# stops reconciling/relaunching. If KernelSU already removed the module entry the
# touch fails; a missing module dir is itself disabled evidence, so never let
# that abort the identity-bound release below.
touch "$MODDIR/remove" 2>/dev/null || true
# 2.1.2 F4: direct fail-closed release BEFORE the controller loop. Waiting for
# the controller lock can take up to 60s while the candidate keeps grabbing the
# panel. --release-now deliberately bypasses the controller and the watchdog
# kill switches, but still requires platform-tool "same" and only clears the
# journals after an identity-bound terminate. REFUSED leaves them for the
# background monitor or the manual fallback documented in BUILD-NOTES §7.4.
if [ -x "$S/runtime-monitor.sh" ]; then
 "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S" --release-now >> "$S/removal-release.log" 2>&1 || true
fi
n=0
while [ "$n" -lt 60 ]; do
 "$S/module-controller" run "$MODDIR" "$S" "$S/platform.sh" && rc=0 || rc=$?
 if [ "$rc" != 73 ]; then break; fi
 sleep 1; n=$((n+1))
done
# KernelSU may run this script from an environment where the module entry is
# already gone; we still got to run the release hook above because it only needs
# the persistent state dir. If it did not run at all (no runtime-ready, not in
# the ksu domain, or no runtime-monitor.sh), use the manual fallback in
# BUILD-NOTES §7.4 (same -> terminate -> clear journals).
if [ "$rc" != 73 ]; then
 if [ -e "$S/pending" ] || [ -e "$S/recovery-owner" ] || [ -e "$S/candidate" ]; then
  "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S" </dev/null >> "$S/removal-monitor.log" 2>&1 &
 fi
 exit "$rc"
fi
exit 75
