#!/system/bin/sh
# Runs from persistent own state, not a soon-to-be-removed module directory.
set -eu
M=$1
S=$2
# Covers all callers, including service reentry and uninstall's retained owner.
if [ "${3:-}" != --monitor-locked ]; then
 exec "$S/module-controller" monitor "$M" "$S" "$0"
fi
failures=0
# A breaker that only counts samples (3 x ~2s) cannot tell a boot/plug-in
# transient from a real fault: HAL restart, module-dir swap and USB churn all
# return non-zero for a few seconds. Require the streak to also span this many
# seconds so a transient can never disable the module, while a genuine fault
# still trips the breaker. Tests narrow it; production keeps the default.
window=${MIPAD_MONITOR_DISABLE_WINDOW:-30}
first_failure=0
epoch=
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
while :; do
 refresh_epoch
 run_epoch=$epoch
 "$S/module-controller" run "$M" "$S" "$S/platform.sh" >> "$S/supervisor.log" 2>&1 && rc=0 || rc=$?
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
