#!/system/bin/sh
# Production platform capability. Never called during packaging/tests.
# Only controller's lock-owning guardian / serialized worker invokes this.
set -eu
umask 077
OP=$1
S=$2
M=$3
# Opt-in local diagnostic contract; stderr is durably captured by supervisor.
# Never enabled on a live device by installation or by this investigation.
if [ "${MIPAD_LIFECYCLE_TRACE:-0}" = 1 ]; then
 PS4='+ lifecycle op=$OP pid=$$ line=${LINENO:-unknown}: '
 set -x
fi
T=$S/platform-tool
HAL=/odm/bin/vendor.xiaomi.usb.dp-service
STAGE=/dev/mipad-fullsplit-v104-local
DSO=$STAGE/fullsplit-bounded-v2.so
CLIENT=$S/prepare-owner
. "$S/profile.sh"
. "$S/common.sh"
STATE=$S/svid
# sysfs type associates on tmpfs, not labeledfs (/data). Keep journals on data.
# Existing stage is independently authenticated; never recreate or replace a mapped DSO.
FAKE=$STAGE/fake_svid
OWNER=$STATE/owner
LOCK=$STATE/lock
log() { printf '%s %s\n' "$(date +%s)" "$*" >> "$S/lifecycle.log"; }
guard() {
 [ "$(id -u)" = 0 ] && [ "$(id -Z)" = u:r:ksu:s0 ] || return 1
 [ "$(getenforce)" = Enforcing ] || return 1
 [ "$(getprop ro.product.cpu.abi)" = arm64-v8a ] || return 1
 profile_check && global_check
}
svid_action() {
 # Native controller flock serializes all callers; no kill-stale mkdir lock.
 global_check && [ ! -L "$STATE" ] || return 1
 mkdir -p "$STATE"; chmod 700 "$STATE"
 case "$1" in
 enable)
  if [ -z "$(entry)" ]; then
   [ "$(sha256sum "$TARGET" | cut -d ' ' -f1)" = 3d1da549cf326dddc07c8684becda1c25aa58f9306f03431fa5d4ed8a5e0a92f ] || return 1
  fi
  rm -f "$STATE/off"; apply;;
 disable) touch "$STATE/off"; remove_owned;;
 *) return 1;;
 esac
}
record_hal() {
 "$T" find "$HAL" > "$S/hal.new" || return 1
 mv "$S/hal.new" "$S/hal"
}
descriptors_save() {
 for i in 0 1; do
  p=/config/usb_gadget/g1/functions/hid.usb$i/report_desc
  [ -r "$p" ] || return 1
  cp "$p" "$S/descriptor$i" || return 1
  cat /config/usb_gadget/g1/functions/hid.usb$i/report_length > "$S/descriptor-length$i" || return 1
 done
}
# Original baseline is rollback evidence, not a prerequisite native interface.
descriptors_check() {
 for i in 0 1; do
  cmp -s "$S/descriptor$i" /config/usb_gadget/g1/functions/hid.usb$i/report_desc || return 1
  cmp -s "$S/descriptor-length$i" /config/usb_gadget/g1/functions/hid.usb$i/report_length || return 1
 done
}
candidate_descriptors_check() { known_descriptors_check 62; }
known_descriptors_check() {
 # Only reviewed factory (9/10) and native (9/62) shapes share these bytes.
 [ "$(cat /config/usb_gadget/g1/functions/hid.usb0/report_length)" = 9 ] || return 1
 [ "$(cat /config/usb_gadget/g1/functions/hid.usb1/report_length)" = "$1" ] || return 1
 [ "$(sha256sum /config/usb_gadget/g1/functions/hid.usb0/report_desc | cut -d ' ' -f1)" = 0ac38b057ec9608b20557ce069c520e1789bf8a597059126dc76d464f7599755 ] || return 1
 desc1=$(sha256sum /config/usb_gadget/g1/functions/hid.usb1/report_desc | cut -d ' ' -f1) || return 1
 # Factory Pen is 107 bytes. Reviewed retained native descriptor bytes may
 # also survive with the factory length; native activation still requires 62.
 case "$1:$desc1" in
  10:e743412bc1096b18566ad74f9bc46aa40fd2bde608fde05fbe8e2c5531031fa1|10:2235bd04d655a092c83b7dfb3915ba67b4b63ec4a1f0ef332851bbed5bdf3a25|62:2235bd04d655a092c83b7dfb3915ba67b4b63ec4a1f0ef332851bbed5bdf3a25) return 0;;
  *) return 1;;
 esac
}
stage() {
 [ "$(stat -f -c %T /dev)" = tmpfs ] || return 1
 if [ -e "$STAGE" ]; then
  [ ! -L "$STAGE" ] && [ "$(stat -c '%u:%g:%a' "$STAGE")" = 0:0:755 ] || return 1
 else mkdir "$STAGE"; chmod 755 "$STAGE"; chcon u:object_r:vendor_file:s0 "$STAGE"; fi
 [ "$(ls -Zd "$STAGE" | awk '{print $1}')" = u:object_r:vendor_file:s0 ] || return 1
 if [ ! -e "$DSO" ]; then
  cp "$S/fullsplit-bounded-v2.so" "$DSO.new" && chmod 644 "$DSO.new" && chcon u:object_r:vendor_file:s0 "$DSO.new" && mv "$DSO.new" "$DSO" || return 1
 fi
 # The staged DSO must be exactly the packaged runtime DSO; bootstrap.sh
 # wrote and verified it through runtime.sha256 before this path runs.
 # package_check.py cross-checks this pin against the packaged DSO.
 [ ! -L "$DSO" ] && [ "$(sha256sum "$DSO" | cut -d ' ' -f1)" = 72757954666bcca58a022c546f81fefb1bb7ffef4a3250fcc9236982bc79c488 ] || return 1
 [ "$(ls -Z "$DSO" | awk '{print $1}')" = u:object_r:vendor_file:s0 ]
}
verify_candidate() { "$T" verify "$S/candidate" "$S/candidate-owner" hooked "$DSO" && candidate_descriptors_check; }
launch_owner() { "$T" launch "${2:-owner}" "$CLIENT" - - "$1"; }
lifecycle_release() { "$CLIENT" --release; }
wait_verify() {
 n=0
 while [ "$n" -lt 50 ]; do
  if "$T" verify "$1" "$2" "$3" "$DSO"; then
   if [ "$3" = original ]; then descriptors_check && return 0
   else candidate_descriptors_check && return 0; fi
  fi
  sleep 0.1; n=$((n+1))
 done
 return 1
}
# Typed outcome 78 is authenticated quiescence, NOT recovered ownership and
# NOT a claim about a physical DP cable. No prepare/stop/terminate in this path.
waiting_descriptors_check() {
 # Saved rollback bytes alone cannot authenticate an unknown gadget shape.
 # A disconnected HAL may retain either reviewed factory or native lengths.
 if candidate_descriptors_check || known_descriptors_check 10; then return 0; fi
 # Only fully absent functions (with a readable parent) count as uncreated.
 p=/config/usb_gadget/g1/functions
 [ -r "$p" ] && [ ! -e "$p/hid.usb0" ] && [ ! -e "$p/hid.usb1" ]
}
# Read-only counterpart of remove_owned/recover_mount_intent. Never repair a
# record to find out whether it was ours; an off flag cannot hide a foreign bind.
wait_mount_check() (
 [ ! -L "$STATE" ] && [ ! -L "$FAKE" ] || return 1
 mid=$(entry) || return 1
 case "$mid" in *[!0-9]*) return 1;; esac
 has_owner=0; has_intent=0
 if [ -e "$OWNER" ] || [ -L "$OWNER" ]; then
  has_owner=1
  [ -f "$OWNER" ] && [ ! -L "$OWNER" ] && [ "$(wc -l < "$OWNER" | tr -d ' ')" = 5 ] || return 1
  { IFS= read -r ob; IFS= read -r on; IFS= read -r oi; IFS= read -r of; IFS= read -r ot; } < "$OWNER" || return 1
  case "$oi" in ''|*[!0-9]*) return 1;; esac
  [ -n "$ob" ] && [ -n "$on" ] && [ -n "$of" ] && [ "$of" = "$ot" ] &&
   [ "$of" = "$(identity "$FAKE")" ] && [ "$(cat "$FAKE")" = 0 ] || return 1
 fi
 if [ -e "$STATE/mount-intent" ] || [ -L "$STATE/mount-intent" ]; then
  has_intent=1
  [ -f "$STATE/mount-intent" ] && [ ! -L "$STATE/mount-intent" ] &&
   [ "$(wc -l < "$STATE/mount-intent" | tr -d ' ')" = 3 ] || return 1
  { IFS= read -r ib; IFS= read -r ins; IFS= read -r iid; } < "$STATE/mount-intent" || return 1
  [ -n "$ib" ] && [ -n "$ins" ] && [ -n "$iid" ] &&
   [ "$iid" = "$(identity "$FAKE")" ] && [ "$(cat "$FAKE")" = 0 ] || return 1
 fi
 # No bind: known journals may survive unmount/crash/reboot, as in common.sh.
 # They are not live ownership and are preserved here, including off+intent.
 [ -n "$mid" ] || return 0
 if [ "$has_owner" = 1 ]; then owned || return 1
 else [ "$has_intent" = 1 ] || return 1; fi
 if [ "$has_intent" = 1 ]; then
  [ "$ib" = "$(boot)" ] && [ "$ins" = "$(ns)" ] &&
   [ "$iid" = "$(identity "$TARGET")" ] && [ "$(cat "$TARGET")" = 0 ] || return 1
 fi
)
wait_probe() {
 wait_mount_check || return 1
 # All retained journals must remain interpretable; do not erase stale evidence.
 preexec=0
 for name in candidate candidate-owner recovery-owner recovery-owner-replacement; do
  if [ -e "$S/$name" ] || [ -L "$S/$name" ]; then
   expected=$CLIENT; [ "$name" != candidate ] || expected=$HAL
   journal_rc=0; "$T" journal "$S/$name" "$expected" || journal_rc=$?
   case "$journal_rc" in 0) :;; 80) preexec=1;; *) return 1;; esac
  fi
 done
 # Exact preexec identity is allowed ONLY back to the pre-existing cleanup.
 [ "$preexec" = 0 ] || return 80
 found=0; "$T" find "$HAL" > "$S/wait-hal" || found=$?
 # No HAL is not quiescence. Recovery may follow its existing controlled
 # start path, but ambiguity/read failures must never reach that path.
 if [ "$found" = 2 ]; then return 79; fi
 [ "$found" = 0 ] || return 1
 role=original
 if [ -f "$S/candidate" ] && cmp -s "$S/candidate" "$S/wait-hal"; then role=hooked; fi
 rc=0; "$T" quiescent "$S/wait-hal" "$role" "$DSO" || rc=$?
 wait_kind=quiescence
 if [ "$rc" = 1 ]; then
  # A live pidfd without endpoints is not idle. Accept only an exact current
  # journaled prepare generation referenced by that HAL, twice authenticated.
  # This never asserts ownership success or launches another prepare client.
  for client_record in candidate-owner recovery-owner recovery-owner-replacement; do
   [ -f "$S/$client_record" ] || continue
   owner_rc=0; "$T" quiescent "$S/wait-hal" "$role" "$DSO" "$S/$client_record" "$CLIENT" || owner_rc=$?
   if [ "$owner_rc" = 78 ]; then rc=78; wait_kind=prepare_in_progress; break; fi
  done
 fi
 case "$rc" in
  0) return 0;;
  78) waiting_descriptors_check || return 1
      log "WAIT_AUTHENTICATED kind=$wait_kind role=$role; ownership unverified; journals retained; no prepare"
      return 78;;
  *) return 1;;
 esac
}
# --- 2.1.5 handover quiescence (T2) ----------------------------------------
# 2.1.4 mounted the SVID override first, then released and stopped the stock
# HAL and immediately ran find/launch: the candidate could exec while init had
# not yet proven the stock HAL stopped and the old hidg consumers were still
# live. A cold reboot (bootreason AP_S_COLDBOOT, pstore pmsg only) was observed
# inside exactly that window; the root cause is still under T1 investigation.
# This is a timing hardening of that transient window, NOT a claimed
# root-cause fix. The SVID mount and the candidate launch now happen only
# after the stop is acknowledged by init, both hidg endpoints are gone and a
# quiet window has elapsed. Every wait is bounded: the diagnostic env knobs
# may only shrink the window inside the reviewed envelope below.
handover_tenths() { # raw_ms default_ms max_ms -> 0.1s ticks (clamped)
 _raw=$1
 case "$_raw" in ''|*[!0-9]*) _raw=$2;; esac
 [ "$_raw" -ge 100 ] || _raw=$2
 [ "$_raw" -le "$3" ] || _raw=$3
 printf '%s\n' $((_raw / 100))
}
# Fail-closed undo for a partially attempted handover. Never reissues ctl.stop
# and never loops back into the stop path: it restores the stock HAL best
# effort (bounded), removes an SVID override it had applied and terminates
# only the candidate/owner journals this invocation launched. The pending
# journal is deliberately retained: recover()/the watchdog own the rest of the
# fail-closed cleanup.
handover_abort() { # reason svid_applied launched detail
 _reason=$1; _svid=$2; _launched=$3; _detail=${4:-none}
 if [ "$_launched" = 1 ]; then
  if [ -f "$S/candidate" ]; then
   "$T" terminate "$S/candidate" >/dev/null 2>&1 || log 'HANDOVER_ABORT_UNDONE step=candidate'
  fi
  if [ -f "$S/candidate-owner" ]; then
   "$T" terminate "$S/candidate-owner" >/dev/null 2>&1 || log 'HANDOVER_ABORT_UNDONE step=candidate-owner'
  fi
  rm -f "$S/candidate.new" "$S/candidate-owner.new"
 fi
 if [ "$_svid" = 1 ]; then
  svid_action disable >/dev/null 2>&1 || log 'HANDOVER_ABORT_UNDONE step=svid_disable'
 fi
 # One bounded stock-HAL restore attempt: a running HAL ignores start, a
 # stopped one gets a single start (never a second ctl.stop, no retry loop
 # around the stop itself).
 setprop ctl.start vendor.usb.dp.hal >/dev/null 2>&1 || log 'HANDOVER_ABORT_UNDONE step=ctl_start'
 _restored=0
 _n=0
 while [ "$_n" -lt 20 ]; do
  if record_hal >/dev/null 2>&1 && "$T" original "$S/hal" >/dev/null 2>&1; then
   _restored=1
   break
  fi
  sleep 0.1
  _n=$((_n + 1))
 done
 if [ "$_restored" = 1 ]; then _detail="$_detail,stock=restored"; else _detail="$_detail,stock=unverified"; fi
 log "HANDOVER_ABORT reason=$_reason detail=$_detail svid_rollback=$_svid launched=$_launched pending=retained"
 return 0
}
recover() {
 [ -e "$S/pending" ] || [ -f "$S/recovery-owner" ] || return 0
 # Probe BEFORE candidate termination, SVID changes or owner replacement.
 if [ -e "$S/pending" ]; then oldboot=$(cat "$S/pending"); else oldboot=$(cat "$S/owner-boot"); fi
 if [ "$oldboot" = "$(boot)" ]; then
  probe=0; wait_probe || probe=$?
  case "$probe" in 0|79|80) :;; *) return "$probe";; esac
 fi
 log 'recovery begin; controller journal retained until ownership verified'
 if [ -e "$S/pending" ]; then oldboot=$(cat "$S/pending"); else oldboot=$(cat "$S/owner-boot"); fi
 if [ "$oldboot" = "$(boot)" ]; then
  if [ -f "$S/candidate" ]; then
   if "$T" same "$S/candidate"; then
    # Release only our exact active HAL before termination; its factory method
    # removes persistent gadget links without any external configfs writer.
    if "$T" verify "$S/candidate" "$S/candidate-owner" hooked "$DSO"; then
     lifecycle_release || return 1
    fi
   fi
   "$T" terminate "$S/candidate" || return 1
  fi
 else
  # A true new boot invalidates all process identities, never signal old PIDs.
  rm -f "$S/candidate" "$S/candidate-owner" "$S/recovery-owner"
 fi
 svid_action disable || return 1
 # Never stop an independently restarted init HAL or adopt an unknown hook.
 if ! record_hal; then
  # find=2 means no process; find=3 means ambiguous and must fail closed.
  rc=0; "$T" find "$HAL" >/dev/null || rc=$?
  [ "$rc" = 2 ] || return 1
  setprop ctl.start vendor.usb.dp.hal
  n=0; until record_hal; do n=$((n+1)); [ "$n" -lt 50 ] || return 1; sleep 0.1; done
 fi
 "$T" original "$S/hal" || return 1
 # If the system or retained recovery client already owns both endpoints, keep it.
 if "$T" baseline "$S/hal" > "$S/current-owner" && descriptors_check; then
  descriptors_check || return 1
  read -r current current_start < "$S/current-owner"
  # A prepare started before idle waiting can become the real owner later.
  # Promote its already-verified journal before clearing pending, so uninstall
  # and disabled monitoring retain the exact live recovery dependency.
  if [ -f "$S/recovery-owner-replacement" ]; then
   if "$T" verify "$S/hal" "$S/recovery-owner-replacement" original "$DSO"; then
    if [ -f "$S/recovery-owner" ]; then
     read -r retained retained_start ignored < "$S/recovery-owner"
     if [ "$retained:$retained_start" != "$current:$current_start" ]; then "$T" terminate "$S/recovery-owner" || return 1; fi
    fi
    mv "$S/recovery-owner-replacement" "$S/recovery-owner"; boot > "$S/owner-boot"; sync
   else
    "$T" terminate "$S/recovery-owner-replacement" || return 1
    rm -f "$S/recovery-owner-replacement"
   fi
  fi
 else
  # Liveness alone is not ownership: a client can outlive its old Binder HAL.
  # Keep the stale client journal until a replacement owns the current HAL.
  if [ -f "$S/recovery-owner" ] && "$T" same "$S/recovery-owner" &&
     wait_verify "$S/hal" "$S/recovery-owner" original; then
   :
  else
   replacement=$S/recovery-owner-replacement
   if [ -f "$replacement" ]; then "$T" terminate "$replacement" || return 1; fi
   rm -f "$replacement" "$replacement.new"
   boot > "$S/owner-boot"; sync
   launch_owner "$replacement" owner-rebuild || return 1
   wait_verify "$S/hal" "$replacement" original || return 1
   if [ -f "$S/recovery-owner" ]; then "$T" terminate "$S/recovery-owner" || return 1; fi
   mv "$replacement" "$S/recovery-owner"; sync
  fi
  read -r current current_start ignored < "$S/recovery-owner"
 fi
 # New durable ownership must be proven BEFORE retiring our prior prepare client.
 if [ -f "$S/candidate-owner" ]; then
  read -r prior prior_start ignored < "$S/candidate-owner"
  if [ "$prior:$prior_start" != "$current:$current_start" ]; then "$T" terminate "$S/candidate-owner" || return 1; fi
 fi
 if [ -f "$S/recovery-owner" ]; then
  read -r retained retained_start ignored < "$S/recovery-owner"
  if [ "$retained:$retained_start" != "$current:$current_start" ]; then
   "$T" terminate "$S/recovery-owner" || return 1
   rm -f "$S/recovery-owner" "$S/owner-boot"
   log 'system owner takeover verified; retained client retired'
  fi
 fi
 rm -f "$S/pending" "$S/candidate" "$S/candidate-owner" "$S/candidate.new" "$S/candidate-owner.new"
 sync
 log 'recovery verified; original identity, two hidg FDs, live pidfd owner, descriptors; owner retained'
}
reconcile() {
 [ -d "$M" ] && [ ! -e "$M/disable" ] && [ ! -e "$M/remove" ] || return 1
 if [ -e "$S/pending" ]; then verify_candidate || return 1; return 0; fi
 # No destructive action before dynamic baseline and an armed guardian exist.
 record_hal || return 1
 baseline_rc=0; "$T" baseline "$S/hal" > "$S/baseline-owner" || baseline_rc=$?
 if [ "$baseline_rc" != 0 ]; then
  # baseline=2 is ambiguous (no endpoints OR invalid owner). Never equate it
  # with waiting: only the stronger, read-only classifier can return 78.
  probe=0; wait_probe || probe=$?
  [ "$probe" = 78 ] && return 78
  # probe=0: the gadget is active (both endpoints) but no verifiable owner is
  # recorded yet. That is the plug-in / prepare window, not a fault: three
  # samples inside ~5s would otherwise write `disable` before ownership exists.
  # Nothing is mutated here, so waiting is strictly safer than failing.
  if [ "$probe" = 0 ] && waiting_descriptors_check; then
   log 'WAIT_ACTIVE kind=active role=original; endpoints present; ownership unverified; journals retained; no prepare'
   return 78
  fi
  return 1
 fi
 descriptors_save && stage || return 1
 boot > "$S/pending.new" && mv "$S/pending.new" "$S/pending" && sync || return 1
 # The recorded factory HAL must still be the authenticated original before
 # anything is torn down.
 "$T" same "$S/hal" && "$T" original "$S/hal" || return 1
 # Factory HAL owns all gadget writes; release its old gadget before stop.
 lifecycle_release || return 1
 # 2.1.5 handover: stop -> init/gadget quiescence -> quiet window -> SVID
 # mount -> unique-absence check -> candidate launch. See the block comment on
 # handover_abort() for why the SVID mount moved behind the stop.
 ht_stop=$(handover_tenths "${MIPAD_DP_HANDOVER_STOP_MS:-}" 4000 5000)
 ht_quiet=$(handover_tenths "${MIPAD_DP_HANDOVER_QUIET_MS:-}" 800 2000)
 ht_total=$(handover_tenths "${MIPAD_DP_HANDOVER_TOTAL_MS:-}" 8000 8000)
 [ $((ht_stop + ht_quiet)) -le "$ht_total" ] || ht_total=$((ht_stop + ht_quiet))
 hticks=0
 svc=unknown
 log "HANDOVER_START stop_tenths=$ht_stop quiet_tenths=$ht_quiet total_tenths=$ht_total"
 if ! setprop ctl.stop vendor.usb.dp.hal; then
  handover_abort stop_rejected 0 0 ctl_stop
  return 1
 fi
 # Bounded stop wait: init must report stopped, the authenticated old PID must
 # be gone and both hidg endpoints must disappear. A HAL that comes back after
 # the stop was acknowledged is a restart, not a slow teardown: abort once,
 # never issue a second stop.
 stop_seen=0
 stop_ok=0
 ht_limit=$ht_stop
 [ "$ht_limit" -le "$((ht_total - hticks))" ] || ht_limit=$((ht_total - hticks))
 while [ "$hticks" -lt "$ht_limit" ]; do
  svc=$(getprop init.svc.vendor.usb.dp.hal) || svc=unknown
  case "$svc" in
   stopped) stop_seen=1;;
   running)
    if [ "$stop_seen" = 1 ]; then
     handover_abort hal_restarted 0 0 init.svc=running-after-stopped
     return 1
    fi;;
  esac
  if [ "$stop_seen" = 1 ] && [ ! -e /dev/hidg0 ] && [ ! -e /dev/hidg1 ] && "$T" gone "$S/hal"; then
   stop_ok=1
   break
  fi
  sleep 0.1
  hticks=$((hticks + 1))
 done
 if [ "$stop_ok" != 1 ]; then
  hidg0=present; [ -e /dev/hidg0 ] || hidg0=gone
  hidg1=present; [ -e /dev/hidg1 ] || hidg1=gone
  handover_abort stop_timeout 0 0 "svc=$svc,hidg0=$hidg0,hidg1=$hidg1"
  return 1
 fi
 # Quiet window: hold the quiescent state for a bounded interval so init's
 # post-stop teardown and the USB controller deconfiguration settle before
 # the SVID mount and the candidate exec (0.1s granularity). The whole
 # stop+quiet pre-launch window stays inside the total cap.
 ht_limit=$ht_quiet
 [ "$ht_limit" -le "$((ht_total - hticks))" ] || ht_limit=$((ht_total - hticks))
 qticks=0
 while [ "$qticks" -lt "$ht_limit" ]; do
  svc=$(getprop init.svc.vendor.usb.dp.hal) || svc=unknown
  if [ "$svc" = running ]; then
   handover_abort hal_restarted 0 0 init.svc=running-during-quiet
   return 1
  fi
  sleep 0.1
  qticks=$((qticks + 1))
  hticks=$((hticks + 1))
 done
 # A gadget that reappeared in the quiet window means something reconfigured
 # USB after init claimed the service stopped: treat it exactly like a restart.
 if [ -e /dev/hidg0 ] || [ -e /dev/hidg1 ]; then
  handover_abort hal_restarted 0 0 hidg_reappeared
  return 1
 fi
 if [ "$hticks" -gt "$ht_total" ]; then
  handover_abort budget 0 0 "ticks=$hticks"
  return 1
 fi
 # SVID override only after the gadget is provably quiet. apply() verifies
 # ownership; read the override back explicitly before any launch.
 if ! svid_action enable; then
  handover_abort svid_enable 1 0 apply_failed
  return 1
 fi
 if ! owned || [ "$(cat "$FAKE")" != 0 ]; then
  handover_abort svid_verify 1 0 readback
  return 1
 fi
 # No second HAL: require unique discovery to report absent immediately pre-launch.
 rc=0; "$T" find "$HAL" >/dev/null || rc=$?
 if [ "$rc" != 2 ]; then
  handover_abort hal_present 1 0 "find_rc=$rc"
  return 1
 fi
 if ! "$T" launch hal "$HAL" "$DSO" "$STAGE" "$S/candidate"; then
  handover_abort launch 1 0 launch_failed
  return 1
 fi
 # Binder registration may lag exec, so owner retries are bounded and each is tracked.
 n=0
 while [ "$n" -lt 20 ]; do
  if [ -f "$S/candidate-owner" ] && "$T" same "$S/candidate-owner"; then break; fi
  rm -f "$S/candidate-owner" "$S/candidate-owner.new"
  if ! launch_owner "$S/candidate-owner"; then
   handover_abort owner_launch 1 1 launch_owner_failed
   return 1
  fi
  sleep 0.2; n=$((n+1))
 done
 if ! wait_verify "$S/candidate" "$S/candidate-owner" hooked; then
  handover_abort candidate_verify 1 1 verify_timeout
  return 1
 fi
 if [ -f "$S/recovery-owner" ]; then
  [ "$(cat "$S/owner-boot")" = "$(boot)" ] || return 1
  "$T" terminate "$S/recovery-owner" || return 1
  rm -f "$S/recovery-owner" "$S/owner-boot"
 fi
 log "HANDOVER_DONE stop_tenths=$ht_stop quiet_tenths=$ht_quiet elapsed_tenths=$hticks"
 log 'candidate ready; dynamic PID+starttime, HAL domain, AT_SECURE=0, DSO maps, two FDs and live prepare owner verified'
}
guard || { log 'profile/domain/namespace guard rejected; no mutation'; exit 70; }
case "$OP" in recover) recover;; reconcile) reconcile;; *) exit 64;; esac
