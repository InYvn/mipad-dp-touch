#!/system/bin/sh
# Runs from persistent own state, not a soon-to-be-removed module directory.
set -eu
M=$1
S=$2
# Covers all callers, including service reentry and uninstall's retained owner.
# --monitor-locked is the normal loop. --release-now is the 2.1.2 uninstall hook:
# it deliberately bypasses the module-controller and the watchdog kill switches
# so removal cannot leave a journaled grabber behind (F4).
case "${3:-}" in
 --monitor-locked|--release-now) :;;
 *) exec "$S/module-controller" monitor "$M" "$S" "$0";;
esac
failures=0
# A breaker that only counts samples (3 x ~2s) cannot tell a boot/plug-in
# transient from a real fault: HAL restart, module-dir swap and USB churn all
# return non-zero for a few seconds. Require the streak to also span this many
# seconds so a transient can never disable the module, while a genuine fault
# still trips the breaker. Tests narrow it; production keeps the default.
window=${MIPAD_MONITOR_DISABLE_WINDOW:-30}
first_failure=0
epoch=
no_hal_seen=
identity_key() {
 # BusyBox/Android stat first; BSD fallback keeps the host test exact.
 stat -c '%d:%i' "$1" 2>/dev/null || stat -f '%d:%i' "$1" 2>/dev/null || printf unknown
}
enable_epoch() {
 if [ ! -d "$M" ] || [ -e "$M/disable" ] || [ -e "$M/remove" ]; then printf disabled; return; fi
 printf 'enabled:%s:%s' "$(identity_key "$M")" "$(identity_key "$M/module.prop")"
}
refresh_epoch() {
 next=$(enable_epoch)
 if [ "$next" != "$epoch" ]; then
  failures=0; first_failure=0; epoch=$next
  printf '%s MONITOR_EPOCH monitor=%s failures=0 epoch=%s\n' "$(date +%s)" "$$" "$epoch" >> "$S/supervisor.log"
 fi
}

# ---------------------------------------------------------------------------
# 2.1.2 DP lifecycle watchdog (corrections for 2.1.1: BUILD-NOTES §0; the
# F1/F2/F4 test matrix is BUILD-NOTES §11).
#
# The hooked DP candidate is the stock /odm/bin/vendor.xiaomi.usb.dp-service
# started per DP-in session; it holds an EVIOCGRAB on the physical touchpad
# (event11), so it must exit as soon as the DP-in session ends. 2.1.0 had no
# path that terminated it after the session: the panel input switch / Mac-side
# stop left the candidate grabbing the touchpad while Android's own touchscreen
# (event6) delivered no usable events. This watchdog releases ONLY the candidate
# this module journaled and launched, and only after the session is provably
# over. Invariants, in order of importance:
#   * Never signal anything without a journal ($S/candidate) that platform-tool
#     "same" still authenticates: pid + starttime + exe must match the record.
#     A stock HAL, a recycled pid, an unrelated process or a preexec journal is
#     therefore never a target. There is no process-name scan anywhere here.
#   * Presence is evidence-based, not DRM-only. T4 found card0-DP-1 reporting
#     "disconnected/disabled" while a Mac session was live at 3408x2272@120
#     (DSI connected, Type-C host/source, configfs hid.usb0/1 present, UDC
#     configured, candidate running). A disconnected DRM connector is therefore
#     never absence evidence; only "connected" is present evidence.
#     2.1.6 (exit-release FAIL, 2026-11-13 01:40): the UDC state is the
#     authoritative host-data-link evidence. Configfs hid.usb0/1 is NOT: the
#     candidate itself creates those functions, so they survive a cable pull and
#     the 2.1.5 "HID absent + all UDC not attached" conjunction never became
#     true (candidate kept grabbing event11 after the Mac session ended).
#     Absent is now "every readable UDC state is not attached"; present is "any
#     readable UDC state is configured". Only when no UDC state is readable (or
#     the readable set is mixed/unknown) does the monitor fall back to the
#     present-evidence signals (DRM connected, Type-C partner/cable, configfs
#     HID); without one it reports unknown. Unknown opens no release gate.
#     This stays fail-safe for "DP gone but adb USB still connected" (UDC
#     configured -> present, no dp_absent release); the sustained absent window
#     (default 15s) covers transient UDC dips during a take-over.
#   * session_lost is platform-tool verify failure OR a candidate descriptor
#     mismatch (the same 9/62 + report_desc identity platform.sh:68). The
#     descriptor arm is what catches "configfs teardown left the candidate
#     verifying while the panel was switched back"; it is only allowed when the
#     gadget parent is readable (otherwise the answer is unknown, not evidence).
#   * The release helper is bounded: 3s initial wait, then SIGTERM for up to 2s,
#     then SIGKILL for up to 1s, with a done-file confirmation. A helper that
#     ignores TERM must never wedge the monitor (2.1.1 F1).
#   * A remove marker or a missing module directory is itself disabled evidence,
#     and --release-now gives uninstall a direct, fail-closed release hook (2.1.1
#     F4). Both reuse the same identity-bound release below; neither guesses.
#   * Never touch recovery-owner/owner-boot/svid/init services: the normal
#     recover/reconcile paths keep owning those.
#   * Be idempotent: after a release the journals are gone, so the next sample
#     is a no-op, and the stock HAL path is handed back so DP-in can come back
#     through the same reconcile the module always used.
#   * Stay observability-only when a release is refused: every decision is
#     appended to supervisor.log with a WATCHDOG prefix for the device runbook.
# ---------------------------------------------------------------------------
T=$S/platform-tool
CLIENT=$S/prepare-owner
HAL=/odm/bin/vendor.xiaomi.usb.dp-service
DSO=/dev/mipad-fullsplit-v104-local/fullsplit-bounded-v2.so
WLOG=$S/supervisor.log
# Test-only root overrides so the shim harness can model the T4 DP-in layout
# without touching the device. Production leaves both unset (real paths).
SYS=${MIPAD_DP_SYSFS_ROOT:-/sys}
GADGET=${MIPAD_DP_GADGET:-/config/usb_gadget/g1/functions}
# Kill switches for the field: an env override (manual monitor run) or a marker
# file that adb/manager can create without repackaging. Both restore 2.1.0
# behaviour exactly (idle monitor, no probing, no release).
watchdog=${MIPAD_DP_WATCHDOG:-1}
if [ -e "$S/watchdog.off" ]; then watchdog=0; fi
# Absence must persist across consecutive samples; the defaults leave room for
# USB/DP re-enumeration and a Mac-side replug without ever releasing a live
# session. Tests narrow them; production keeps these values.
absent_limit=${MIPAD_DP_ABSENT_SECONDS:-15}
case "$absent_limit" in ''|*[!0-9]*) absent_limit=15;; esac
disabled_limit=${MIPAD_DP_DISABLED_SECONDS:-5}
case "$disabled_limit" in ''|*[!0-9]*) disabled_limit=5;; esac
verify_limit=${MIPAD_DP_VERIFY_SECONDS:-30}
case "$verify_limit" in ''|*[!0-9]*) verify_limit=30;; esac
if [ "$absent_limit" -lt 1 ]; then absent_limit=1; fi
if [ "$disabled_limit" -lt 1 ]; then disabled_limit=1; fi
if [ "$verify_limit" -lt 1 ]; then verify_limit=1; fi
dp_absent_since=0
dp_disabled_since=0
dp_unverified_since=0
# 2.1.4: HID-function teardown is direct session-end evidence, unlike a generic
# verify failure that can also be a stale configfs race. Track it so the release
# can use the single verify window even while Type-C/UDC still report present.
dp_desc_ended=0
dp_last_state=
wlog() { printf '%s WATCHDOG %s\n' "$(date +%s)" "$*" >> "$WLOG"; }
# External DP-in session presence: present|absent|unknown.
# 2.1.6: UDC wins.
#  * any readable UDC state == "configured"       -> present;
#  * every readable UDC state == "not attached"   -> absent (even while the
#    candidate's own configfs HID functions persist: they are self-referential,
#    not host-session evidence);
#  * no readable UDC (or a mixed/unknown set)      -> fall back to the
#    present-evidence signals below; without one -> unknown.
# Fallback present evidence (any one is enough):
#  * DRM DP/HDMI connector status starts with "connected";
#  * a Type-C partner/cable object exists (the cable is attached; T4 measured
#    port0-partner present with data_role=device, so data_role/power_role alone
#    are deliberately not used - they can describe the port, not DP consumption);
#  * configfs has hid.usb0 or hid.usb1.
# A disconnected DRM connector is NOT absence evidence. Unknown never releases.
dp_state() {
 if [ -n "${MIPAD_DP_PRESENT_PROBE:-}" ] && [ -x "${MIPAD_DP_PRESENT_PROBE:-/nonexistent}" ]; then
  if p=$("$MIPAD_DP_PRESENT_PROBE" 2>/dev/null); then :; else p=unknown; fi
  case "$p" in present|absent|unknown) printf '%s' "$p"; return 0;; esac
  printf unknown; return 0
 fi
 # 2.1.6: UDC state is the authoritative host-data-link evidence, so it is
 # collected first and decides present/absent before any configfs HID check.
 udc_seen=0
 udc_configured=0
 udc_absent=1
 for f in "$SYS"/class/udc/*/state; do
  [ -r "$f" ] || continue
  udc_seen=1
  case "$(cat "$f" 2>/dev/null)" in
   configured) udc_configured=1;;
   'not attached') :;;
   *) udc_absent=0;;
  esac
 done
 if [ "$udc_configured" = 1 ]; then printf present; return 0; fi
 if [ "$udc_seen" = 1 ] && [ "$udc_absent" = 1 ]; then printf absent; return 0; fi
 # UDC unreadable or mixed/unknown: fall back to the present-evidence signals.
 # Absence is NOT inferred here; without a positive signal the answer is unknown.
 present=0
 for f in "$SYS"/class/drm/card*-DP*/status "$SYS"/class/drm/card*-HDMI*/status; do
  [ -r "$f" ] || continue
  case "$(cat "$f" 2>/dev/null)" in connected*) present=1;; esac
 done
 for f in "$SYS"/class/typec/port0-partner* "$SYS"/class/typec/port0-cable*; do
  [ -e "$f" ] && present=1
 done
 if [ -r "$GADGET" ] && { [ -e "$GADGET/hid.usb0" ] || [ -e "$GADGET/hid.usb1" ]; }; then
  present=1
 fi
 if [ "$present" = 1 ]; then printf present; return 0; fi
 printf unknown
}
# F2: the platform-tool verify only proves process/fd identity. platform.sh:68
# additionally requires the reviewed gadget descriptor shape, which is what
# distinguishes a live DP session from configfs teardown that left the process
# verifying. Mirror platform.sh's exact candidate shape here (hid.usb0 9 bytes,
# hid.usb1 62 bytes, fixed report_desc hashes; the 10-byte factory hash is NOT a
# candidate shape). Return 0 match, 1 "shape gone/mismatched" with a readable
# gadget parent, 2 unknown (no sha256sum, unreadable parent, or an existing
# function whose metadata cannot be read). Only 1 is release evidence:
# SELinux/transient configfs failures must never release a live Mac session.
candidate_descriptors_check() {
 command -v sha256sum >/dev/null 2>&1 || return 2
 [ -r "$GADGET" ] || return 2
 has0=0
 has1=0
 [ -e "$GADGET/hid.usb0" ] && has0=1
 [ -e "$GADGET/hid.usb1" ] && has1=1
 # Readable parent with neither reviewed function: the candidate's 9/62 shape
 # is gone. A half-present pair is the same teardown evidence (the sustained
 # window in watchdog_tick still has to pass before anything is released).
 if [ "$has0" = 0 ] || [ "$has1" = 0 ]; then return 1; fi
 [ -r "$GADGET/hid.usb0/report_length" ] || return 2
 [ -r "$GADGET/hid.usb0/report_desc" ] || return 2
 [ -r "$GADGET/hid.usb1/report_length" ] || return 2
 [ -r "$GADGET/hid.usb1/report_desc" ] || return 2
 len0=$(cat "$GADGET/hid.usb0/report_length" 2>/dev/null) || return 2
 len1=$(cat "$GADGET/hid.usb1/report_length" 2>/dev/null) || return 2
 [ "$len0" = 9 ] || return 1
 [ "$len1" = 62 ] || return 1
 desc0=$(sha256sum "$GADGET/hid.usb0/report_desc" 2>/dev/null | cut -d ' ' -f1) || return 2
 desc1=$(sha256sum "$GADGET/hid.usb1/report_desc" 2>/dev/null | cut -d ' ' -f1) || return 2
 [ -n "$desc0" ] && [ -n "$desc1" ] || return 2
 [ "$desc0" = 0ac38b057ec9608b20557ce069c520e1789bf8a597059126dc76d464f7599755 ] || return 1
 [ "$desc1" = 2235bd04d655a092c83b7dfb3915ba67b4b63ec4a1f0ef332851bbed5bdf3a25 ] || return 1
 return 0
}
session_lost_evidence() {
 if candidate_descriptors_check; then return 1; else rc=$?; fi
 [ "$rc" = 1 ]
}
# Best-effort graceful gadget release before the identity-bound kill. Bounded by
# design (2.1.2 F1): a helper that ignores SIGTERM must not wedge the monitor.
# A tiny wrapper records the real client pid and a done marker; after 3s we
# SIGTERM, wait up to 2s, SIGKILL, wait up to 1s, and then continue regardless.
# If even the wrapper cannot be reaped, it is killed so the monitor's own child
# table/proc chain stays bounded; the identity-bound terminate below remains the
# authoritative release. The done marker is proof the client disappeared, not
# that exit was graceful.
release_helper() {
 rm -f "$S/.release-helper.pid" "$S/.release-helper.done"
 (
  "$CLIENT" --release >> "$WLOG" 2>&1 &
  c=$!
  printf '%s\n' "$c" > "$S/.release-helper.pid"
  wait "$c" 2>/dev/null || true
  : > "$S/.release-helper.done"
 ) &
 wrapper=$!
 n=0
 while [ "$n" -lt 15 ]; do
  if [ -e "$S/.release-helper.done" ]; then
   wait "$wrapper" 2>/dev/null || true
   rm -f "$S/.release-helper.pid" "$S/.release-helper.done"
   return 0
  fi
  sleep 0.2
  n=$((n+1))
 done
 wlog "RELEASE_HELPER_TIMEOUT"
 # The wrapper writes the client pid immediately after forking it. If the pid
 # file is not visible yet, poll for up to 1s before declaring it stuck: the
 # helper may have finished between the last done-check and this line.
 helper=
 n=0
 while [ "$n" -lt 5 ]; do
  if [ -e "$S/.release-helper.done" ]; then break; fi
  if [ -r "$S/.release-helper.pid" ]; then
   helper=$(cat "$S/.release-helper.pid" 2>/dev/null || true)
   [ -n "$helper" ] && break
  fi
  sleep 0.2
  n=$((n+1))
 done
 if [ -e "$S/.release-helper.done" ]; then
  # It finished on its own just after the grace window: no kill, no wedge.
  wlog "RELEASE_HELPER_LATE"
  wait "$wrapper" 2>/dev/null || true
  rm -f "$S/.release-helper.pid" "$S/.release-helper.done"
  return 0
 fi
 if [ -n "$helper" ] && [ "$helper" != 0 ]; then
  kill -TERM "$helper" 2>/dev/null || true
  n=0
  while [ "$n" -lt 10 ]; do
   if [ -e "$S/.release-helper.done" ]; then break; fi
   sleep 0.2
   n=$((n+1))
  done
  if [ ! -e "$S/.release-helper.done" ]; then
   kill -KILL "$helper" 2>/dev/null || true
   n=0
   while [ "$n" -lt 5 ]; do
    if [ -e "$S/.release-helper.done" ]; then break; fi
    sleep 0.2
    n=$((n+1))
   done
  fi
  if [ -e "$S/.release-helper.done" ]; then
   wlog "RELEASE_HELPER_KILLED"
  else
   # Kernel D-state or an unreaped zombie: never block the monitor on it. Kill
   # our wrapper too; the helper is already unkillable and the candidate
   # terminate below is authoritative.
   wlog "RELEASE_HELPER_STUCK"
   kill -KILL "$wrapper" 2>/dev/null || true
  fi
 else
  wlog "RELEASE_HELPER_STUCK"
  kill -KILL "$wrapper" 2>/dev/null || true
 fi
 # Only reap the wrapper when it has already published done; never wait on a
 # wedged helper. The identity-bound terminate below remains authoritative.
 if [ -e "$S/.release-helper.done" ]; then wait "$wrapper" 2>/dev/null || true; fi
 rm -f "$S/.release-helper.pid" "$S/.release-helper.done"
 return 0
}
restore_stock_hal() {
 reason=$1
 rc=0
 "$T" find "$HAL" >/dev/null 2>&1 || rc=$?
 if [ "$rc" = 0 ]; then return 0; fi
 if [ "$rc" != 2 ]; then wlog "STOCK_HAL_AMBIGUOUS reason=$reason rc=$rc"; return 0; fi
 if ! command -v setprop >/dev/null 2>&1; then wlog "STOCK_HAL_UNAVAILABLE reason=$reason"; return 0; fi
 if ! setprop ctl.start vendor.usb.dp.hal >/dev/null 2>&1; then wlog "STOCK_HAL_START_FAILED reason=$reason"; return 0; fi
 n=0
 rc=0
 while [ "$n" -lt 50 ]; do
  rc=0
  "$T" find "$HAL" >/dev/null 2>&1 || rc=$?
  if [ "$rc" = 0 ]; then break; fi
  sleep 0.1
  n=$((n+1))
 done
 if [ "$rc" = 0 ]; then wlog "STOCK_HAL_RESTORED reason=$reason"; else wlog "STOCK_HAL_START_TIMEOUT reason=$reason"; fi
}
# Every attempt restarts the streak counters, so a refused/failed release is
# retried once per threshold window instead of on every 2s tick (no log storm,
# no tight kill loop). Success is the only path that needs no backoff.
reset_timers() { dp_absent_since=0; dp_disabled_since=0; dp_unverified_since=0; dp_desc_ended=0; }
# The only release path. Caller has already decided the session is over; this
# function re-proves the identity from the journal before any signal and is
# safe to call repeatedly (a stale/absent journal only ever cleans journals it
# can prove dead).
watchdog_release() {
 reason=$1
 cand=$S/candidate
 if [ ! -e "$cand" ]; then
  if [ "$reason" = dp_absent ] && { [ -e "$S/pending" ] || [ -e "$S/candidate-owner" ] || [ -e "$S/candidate.new" ] || [ -e "$S/candidate-owner.new" ]; }; then
   rm -f "$S/pending" "$S/candidate" "$S/candidate-owner" "$S/candidate.new" "$S/candidate-owner.new"
   sync 2>/dev/null || true
   wlog "CLEANED reason=$reason detail=no-candidate-journal"
   reset_timers
  fi
  return 0
 fi
 if ! "$T" same "$cand" >/dev/null 2>&1; then
  # Not the journaled live candidate: never signal it. Only drop records that
  # platform-tool proves are dead (exited or starttime changed).
  if "$T" gone "$cand" >/dev/null 2>&1; then
   rm -f "$S/pending" "$S/candidate" "$S/candidate-owner" "$S/candidate.new" "$S/candidate-owner.new"
   sync 2>/dev/null || true
   wlog "CLEANED reason=$reason detail=stale-candidate-journal"
   reset_timers
  else
   wlog "REFUSED reason=$reason detail=candidate-not-live-not-provably-gone"
   reset_timers
  fi
  return 0
 fi
 if "$T" verify "$cand" "$S/candidate-owner" hooked "$DSO" >/dev/null 2>&1; then
  release_helper
 fi
 rc=0
 "$T" terminate "$cand" || rc=$?
 if [ "$rc" != 0 ]; then wlog "TERMINATE_FAILED reason=$reason rc=$rc"; reset_timers; return 0; fi
 n=0
 while [ "$n" -lt 50 ]; do
  if ! "$T" same "$cand" >/dev/null 2>&1; then break; fi
  sleep 0.1
  n=$((n+1))
 done
 if "$T" same "$cand" >/dev/null 2>&1; then wlog "TERMINATE_TIMEOUT reason=$reason"; reset_timers; return 0; fi
 rm -f "$S/pending" "$S/candidate" "$S/candidate-owner" "$S/candidate.new" "$S/candidate-owner.new"
 sync 2>/dev/null || true
 wlog "RELEASED reason=$reason candidate=journaled grab=released"
 reset_timers
 restore_stock_hal "$reason"
}
# F4: deterministic uninstall hook. Runs even with the watchdog disabled and
# bypasses the controller (uninstall may hold the monitor lock). The release
# itself is still identity-bound and fail-closed; REFUSED leaves the journal for
# the background monitor or the documented manual fallback.
if [ "${3:-}" = --release-now ]; then
 wlog "RELEASE_NOW reason=module_removed"
 watchdog_release module_removed
 exit 0
fi
# 2.1.4: a soft reboot keeps boot_id, so journals left by the previous framework
# generation would otherwise be adopted by recover() and a fresh grabber would be
# launched for a session that is already over. A userspace restart cannot leave a
# live prepare owner behind, so drop the journal only when every recorded owner is
# provably dead. A normal monitor reentry with a live session keeps its journals.
session_owner_alive() {
 [ -e "$S/candidate" ] && "$T" same "$S/candidate" >/dev/null 2>&1 && return 0
 [ -e "$S/candidate-owner" ] && "$T" same "$S/candidate-owner" >/dev/null 2>&1 && return 0
 [ -e "$S/recovery-owner" ] && "$T" same "$S/recovery-owner" >/dev/null 2>&1 && return 0
 return 1
}
cleanup_stale_epoch() {
 [ -e "$S/pending" ] || [ -e "$S/candidate" ] || [ -e "$S/candidate-owner" ] || return 0
 if session_owner_alive; then return 0; fi
 # Fail-closed: a journal that is neither authenticated nor provably gone is
 # diagnostics, never something to delete (S4). Only records platform-tool
 # "gone" proves dead may be dropped; then the soft-reboot journal is inert.
 if [ -e "$S/candidate" ] && ! "$T" gone "$S/candidate" >/dev/null 2>&1; then return 0; fi
 if [ -e "$S/candidate-owner" ] && ! "$T" gone "$S/candidate-owner" >/dev/null 2>&1; then return 0; fi
 if [ -e "$S/recovery-owner" ] && ! "$T" gone "$S/recovery-owner" >/dev/null 2>&1; then return 0; fi
 rm -f "$S/pending" "$S/candidate" "$S/candidate-owner" "$S/candidate.new" \
       "$S/candidate-owner.new" "$S/recovery-owner-replacement" \
       "$S/recovery-owner-replacement.new"
 sync 2>/dev/null || true
 wlog "CLEANED reason=stale-epoch detail=no-live-session"
}
cleanup_stale_epoch
# 2.1.4 F8: bound every controller invocation. 2.1.2 could wedge the whole
# monitor (and therefore the watchdog) in a controller child that never returned
# after a DP session teardown; the panel grab then outlived the session. A
# bounded call keeps the watchdog ticking; the deadline is a wait, never a
# failure, and the next tick retries.
controller_run() {
 if command -v setsid >/dev/null 2>&1; then
  setsid "$S/module-controller" run "$M" "$S" "$S/platform.sh" >> "$S/supervisor.log" 2>&1 &
 else
  "$S/module-controller" run "$M" "$S" "$S/platform.sh" >> "$S/supervisor.log" 2>&1 &
 fi
 cpid=$!
 n=0
 while [ "$n" -lt 100 ]; do
  kill -0 "$cpid" 2>/dev/null || break
  sleep 0.2
  n=$((n+1))
 done
 if kill -0 "$cpid" 2>/dev/null; then
  wlog "CONTROLLER_TIMEOUT pid=$cpid"
  kill -TERM "-$cpid" 2>/dev/null || kill -TERM "$cpid" 2>/dev/null || true
  n=0
  while [ "$n" -lt 10 ]; do
   kill -0 "$cpid" 2>/dev/null || break
   sleep 0.2
   n=$((n+1))
  done
  if kill -0 "$cpid" 2>/dev/null; then
   kill -KILL "-$cpid" 2>/dev/null || kill -KILL "$cpid" 2>/dev/null || true
  fi
  wait "$cpid" 2>/dev/null || true
  return 79
 fi
 rc=0
 wait "$cpid" 2>/dev/null || rc=$?
 return "$rc"
}
# Sample the session state and release only after the corresponding streak.
# Three independent, evidence-backed gates:
#   dp_absent       no present evidence, configfs HID gone and UDC detached,
#                   past the absence limit (DRM disconnected is not absence);
#   module_disabled the module is disabled (disable/remove/missing dir, user
#                   action or our own breaker), so it must not keep the touchpad
#                   grabbed;
#   session_lost    the journaled candidate either stopped verifying as the
#                   hooked owner, or no longer matches the reviewed gadget
#                   descriptor shape (platform.sh:68), past the verify limit.
#                   Present evidence raises the window to 2x (stale configfs).
watchdog_tick() {
 st=$1
 now=$(date +%s)
 case "$st" in
  present|absent|unknown) :;;
  *) st=unknown;;
 esac
 if [ "$st" != "$dp_last_state" ]; then dp_last_state=$st; wlog "PROBE state=$st"; fi
 if [ "$st" = absent ]; then
  if [ "$dp_absent_since" = 0 ]; then dp_absent_since=$now; fi
 else
  dp_absent_since=0
 fi
 if [ -e "$M/disable" ] || [ -e "$M/remove" ] || [ ! -d "$M" ]; then
  if [ "$dp_disabled_since" = 0 ]; then dp_disabled_since=$now; fi
 else
  dp_disabled_since=0
 fi
 unverified=0
 why=
 if [ -f "$S/candidate" ]; then
  if "$T" same "$S/candidate" >/dev/null 2>&1; then
   # 2.1.4: the reviewed HID shape is checked independently of verify. A DP
   # teardown commonly invalidates the hooked-owner verify first (hidg fds are
   # gone), and that must still resolve to the single-window descriptor evidence
   # instead of the doubled present window meant for stale configfs races.
   if session_lost_evidence; then
    unverified=1; why=descriptors
   elif ! "$T" verify "$S/candidate" "$S/candidate-owner" hooked "$DSO" >/dev/null 2>&1; then
    unverified=1; why=verify
   fi
  fi
 fi
 if [ "$unverified" = 1 ]; then
  if [ "$dp_unverified_since" = 0 ]; then
   dp_unverified_since=$now
   case "$why" in
    descriptors) dp_desc_ended=1; wlog "PROBE session=ended evidence=descriptors state=$st";;
    *) wlog "PROBE verify=failed state=$st";;
   esac
  fi
 else
  dp_unverified_since=0
  dp_desc_ended=0
 fi
 reason=
 if [ "$dp_absent_since" -gt 0 ] && [ $((now-dp_absent_since)) -ge "$absent_limit" ]; then
  reason=dp_absent
 elif [ "$dp_disabled_since" -gt 0 ] && [ $((now-dp_disabled_since)) -ge "$disabled_limit" ]; then
  reason=module_disabled
 elif [ "$dp_unverified_since" -gt 0 ] && [ "$dp_desc_ended" = 1 ] && [ $((now-dp_unverified_since)) -ge "$verify_limit" ]; then
  # 2.1.4: the reviewed 9/62 HID shape is gone while Type-C/UDC still look
  # present. That is the panel-switched-back teardown, not a transient: the
  # gadget functions only exist while the candidate serves a DP-in session.
  # One verify window is enough; the doubled present window stays for verify-only
  # failures where stale configfs evidence is still plausible.
  reason=session_lost
 elif [ "$dp_unverified_since" -gt 0 ] && [ $((now-dp_unverified_since)) -ge "$verify_limit" ] && [ "$st" != present ]; then
  reason=session_lost
 elif [ "$dp_unverified_since" -gt 0 ] && [ $((now-dp_unverified_since)) -ge $((verify_limit*2)) ]; then
  # Probe still says "present" but the journaled candidate has not verified for
  # twice the window: the configfs/Type-C evidence is stale (it is exactly what
  # a candidate that outlived its DP session leaves behind). Panel safety wins.
  reason=session_lost
 fi
 if [ -n "$reason" ]; then watchdog_release "$reason"; fi
}
while :; do
 refresh_epoch
 if [ "$watchdog" = 1 ]; then
  st=$(dp_state) || st=unknown
  watchdog_tick "$st"
 else
  st=present
 fi
 if [ "$st" = absent ]; then
  # No DP-in session: launching a candidate now would grab the panel for a Mac
  # that is gone, and a failing reconcile would trip the 30s breaker while the
  # module is merely idle. Wait for the session to be re-established; the
  # normal recover/reconcile path takes over again on the next present sample.
  # Keep the original removal exit reachable without a re-launch.
  if { [ ! -d "$M" ] || [ -e "$M/remove" ]; } && [ ! -e "$S/pending" ] && [ ! -e "$S/recovery-owner" ]; then exit 0; fi
  sleep 2; continue
 fi
 # 2.1.4: no OEM DP HAL means no DP-in session has started yet. In tablet mode
 # with the cable attached the UDC is configured, so dp_state reports present and
 # reconcile cannot find a HAL; counting that as a failure would self-disable the
 # module before the user ever enters DP-in. Wait for the stock HAL instead.
 hal_rc=0; "$T" find "$HAL" >/dev/null 2>&1 || hal_rc=$?
 if [ "$hal_rc" = 2 ]; then
  if [ "$no_hal_seen" != 2 ]; then
   no_hal_seen=2
   printf '%s MONITOR_WAIT monitor=%s rc=78 failures=0 reason=no_dp_hal\n' "$(date +%s)" "$$" >> "$S/supervisor.log"
  fi
  failures=0; first_failure=0
  sleep 2; continue
 fi
 if [ "$no_hal_seen" != 0 ]; then no_hal_seen=0; fi
 run_epoch=$epoch
 rc=0
 controller_run || rc=$?
 refresh_epoch
 # A result belongs to the epoch that launched it, not the one visible now.
 # Poll-invisible same-inode disable/enable remains outside this contract.
 if [ "$run_epoch" != "$epoch" ]; then
  printf '%s MONITOR_STALE_RESULT monitor=%s rc=%s failures=0 from=%s to=%s\n' "$(date +%s)" "$$" "$rc" "$run_epoch" "$epoch" >> "$S/supervisor.log"
  sleep 2; continue
 fi
 # Keep the minimal original-owner recovery monitor after remove ONLY while
 # original HAL depends on our prepare client (or rollback is still pending).
 if { [ ! -d "$M" ] || [ -e "$M/remove" ]; } && [ ! -e "$S/pending" ] && [ ! -e "$S/recovery-owner" ]; then exit "$rc"; fi
 if [ "$rc" = 73 ]; then sleep 1; continue; fi
 if [ "$rc" = 78 ]; then
  # Authenticated idle/owned-prepare waiting is not recovered ownership.
  # It breaks a consecutive-failure streak, including a pre-disable streak.
  failures=0; first_failure=0
  printf '%s MONITOR_WAIT monitor=%s rc=78 failures=0 reason=authenticated_wait\n' "$(date +%s)" "$$" >> "$S/supervisor.log"
  sleep 2; continue
 fi
 if [ "$rc" = 79 ]; then
  # 2.1.4 F8: a bounded controller deadline is a wait, not a fault. The watchdog
  # already ticked before this call; counting the timeout as a failure would
  # disable the module exactly when a helper is merely slow.
  failures=0; first_failure=0
  printf '%s MONITOR_CONTROLLER_TIMEOUT monitor=%s rc=79 failures=0\n' "$(date +%s)" "$$" >> "$S/supervisor.log"
  sleep 2; continue
 fi
 if [ "$epoch" = disabled ]; then
  failures=0; first_failure=0
  if [ "$rc" != 0 ]; then
   printf '%s MONITOR_RECOVERY_DEFERRED monitor=%s rc=%s failures=0 epoch=disabled\n' "$(date +%s)" "$$" "$rc" >> "$S/supervisor.log"
  fi
 elif [ "$rc" != 0 ]; then
  failures=$((failures+1))
  now=$(date +%s)
  [ "$first_failure" -gt 0 ] || first_failure=$now
  streak=$((now-first_failure))
  printf '%s MONITOR_FAILURE monitor=%s rc=%s failures=%s streak=%ss disable=%s remove=%s\n' "$now" "$$" "$rc" "$failures" "$streak" "$([ -e "$M/disable" ] && echo 1 || echo 0)" "$([ -e "$M/remove" ] && echo 1 || echo 0)" >> "$S/supervisor.log"
  if [ "$failures" -ge 3 ] && [ "$streak" -ge "$window" ] && [ -d "$M" ] && [ ! -e "$M/disable" ]; then
   printf '%s MONITOR_DISABLE_ATTEMPT monitor=%s rc=%s failures=%s streak=%ss window=%ss reason=controller_failures\n' "$now" "$$" "$rc" "$failures" "$streak" "$window" >> "$S/supervisor.log"
   # Keep the reason with the marker instead of an empty file: a disabled
   # manager entry must be explainable without the (persistent) logs.
   reason="reason=controller_failures rc=$rc failures=$failures streak=${streak}s window=${window}s at=$now"
   if printf '%s\n' "$reason" > "$M/disable"; then
    printf '%s MONITOR_DISABLE_WRITTEN monitor=%s rc=%s failures=%s streak=%ss window=%ss reason=controller_failures\n' "$now" "$$" "$rc" "$failures" "$streak" "$window" >> "$S/supervisor.log"
   else
    printf '%s MONITOR_DISABLE_WRITE_FAILED monitor=%s rc=%s failures=%s reason=controller_failures\n' "$now" "$$" "$rc" "$failures" >> "$S/supervisor.log"
    exit 1
   fi
  fi
 else failures=0; first_failure=0; fi
 sleep 2
done
