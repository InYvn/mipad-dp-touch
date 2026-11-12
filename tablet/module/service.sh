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
. "$MODDIR/common.sh"
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
# --- keep Android touch alive during DP-in (local single-finger) ---
# The vendor DP-in app switches the relay to full relay once the display mode is
# active: the host then owns the whole touchscreen, so Android's own UI (DP-in
# confirmation, volume/brightness panel, power menu) cannot be touched at all.
# The relay's AIDL also exposes a local single-finger mode (setEnabled(false))
# in which the host still receives touch while Android keeps one finger. The
# vendor app flips the mode back on its own events, so this has to stay resident.
keep_local_touch() {
 _svc=vendor.xiaomi.usb.dp.IUsbDpRelay/default
 _last=
 while :; do
  if [ -e "$MODDIR/disable" ]; then exit 0; fi
  if [ -e "$STATE/no_local_touch" ]; then
   _state=opted_out
  elif grep -q " $TARGET " /proc/mounts 2>/dev/null; then
   _out=$(/system/bin/service call "$_svc" 2 2>/dev/null) || _out=
   case "$_out" in
    *"00000000 00000001"*) _state=local ;;
    *"00000000 00000000"*)
     /system/bin/service call "$_svc" 1 i32 0 >/dev/null 2>&1 || :
     _state=full_to_local
     ;;
    *) _state=absent ;;
   esac
  else
   _state=inactive
  fi
  if [ "$_state" != "$_last" ]; then
   echo "$(date '+%Y-%m-%d %H:%M:%S') touch-mode: $_state" >> "$STATE/touch-mode.log" 2>/dev/null || :
   _last=$_state
  fi
  sleep 1
 done
}
keep_local_touch </dev/null >/dev/null 2>&1 &
# Persistent monitor restarts the supervisor/watchdog and retains prepare ownership
# after disable/remove until independently verified system-client takeover.
exec "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$S/runtime-monitor.sh" "$MODDIR" "$S"
