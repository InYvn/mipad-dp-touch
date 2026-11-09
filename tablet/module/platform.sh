#!/system/bin/sh
# Production platform capability. Never called during packaging/tests.
# Only controller's lock-owning guardian / serialized worker invokes this.
set -eu
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
candidate_descriptors_check() {
 # SVID only affects the next prepareGadget; validate AFTER the owner creates it.
 [ "$(cat /config/usb_gadget/g1/functions/hid.usb0/report_length)" = 9 ] || return 1
 [ "$(cat /config/usb_gadget/g1/functions/hid.usb1/report_length)" = 62 ] || return 1
 [ "$(sha256sum /config/usb_gadget/g1/functions/hid.usb0/report_desc | cut -d ' ' -f1)" = 0ac38b057ec9608b20557ce069c520e1789bf8a597059126dc76d464f7599755 ] || return 1
 [ "$(sha256sum /config/usb_gadget/g1/functions/hid.usb1/report_desc | cut -d ' ' -f1)" = 2235bd04d655a092c83b7dfb3915ba67b4b63ec4a1f0ef332851bbed5bdf3a25 ] || return 1
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
 [ ! -L "$DSO" ] && [ "$(sha256sum "$DSO" | cut -d ' ' -f1)" = 053ee9593102f907aa48e6df9a7325b76059a398b1789f45d651c6481a26748e ] || return 1
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
recover() {
 [ -e "$S/pending" ] || [ -f "$S/recovery-owner" ] || return 0
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
 record_hal && "$T" baseline "$S/hal" > "$S/baseline-owner" || return 75
 descriptors_save && stage || return 1
 boot > "$S/pending.new" && mv "$S/pending.new" "$S/pending" && sync || return 1
 svid_action enable || return 1
 "$T" same "$S/hal" && "$T" original "$S/hal" || return 1
 # Factory HAL owns all gadget writes; release its old gadget before stop.
 lifecycle_release || return 1
 setprop ctl.stop vendor.usb.dp.hal || return 1
 n=0; until "$T" gone "$S/hal" && [ "$(getprop init.svc.vendor.usb.dp.hal)" = stopped ]; do n=$((n+1)); [ "$n" -lt 50 ] || return 1; sleep 0.1; done
 # No second HAL: require unique discovery to report absent immediately pre-launch.
 rc=0; "$T" find "$HAL" >/dev/null || rc=$?
 [ "$rc" = 2 ] || return 1
 "$T" launch hal "$HAL" "$DSO" "$STAGE" "$S/candidate" || return 1
 # Binder registration may lag exec, so owner retries are bounded and each is tracked.
 n=0
 while [ "$n" -lt 20 ]; do
  if [ -f "$S/candidate-owner" ] && "$T" same "$S/candidate-owner"; then break; fi
  rm -f "$S/candidate-owner" "$S/candidate-owner.new"
  launch_owner "$S/candidate-owner" || return 1
  sleep 0.2; n=$((n+1))
 done
 wait_verify "$S/candidate" "$S/candidate-owner" hooked || return 1
 if [ -f "$S/recovery-owner" ]; then
  [ "$(cat "$S/owner-boot")" = "$(boot)" ] || return 1
  "$T" terminate "$S/recovery-owner" || return 1
  rm -f "$S/recovery-owner" "$S/owner-boot"
 fi
 log 'candidate ready; dynamic PID+starttime, HAL domain, AT_SECURE=0, DSO maps, two FDs and live prepare owner verified'
}
guard || { log 'profile/domain/namespace guard rejected; no mutation'; exit 70; }
case "$OP" in recover) recover;; reconcile) reconcile;; *) exit 64;; esac
