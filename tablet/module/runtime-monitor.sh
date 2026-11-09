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
while :; do
 "$S/module-controller" run "$M" "$S" "$S/platform.sh" >> "$S/supervisor.log" 2>&1 && rc=0 || rc=$?
 # Keep the minimal original-owner recovery monitor after remove ONLY while
 # original HAL depends on our prepare client (or rollback is still pending).
 if { [ ! -d "$M" ] || [ -e "$M/remove" ]; } && [ ! -e "$S/pending" ] && [ ! -e "$S/recovery-owner" ]; then exit "$rc"; fi
 if [ "$rc" = 73 ]; then sleep 1; continue; fi
 if [ "$rc" != 0 ]; then
  failures=$((failures+1))
  printf '%s MONITOR_FAILURE monitor=%s rc=%s failures=%s disable=%s remove=%s\n' "$(date +%s)" "$$" "$rc" "$failures" "$([ -e "$M/disable" ] && echo 1 || echo 0)" "$([ -e "$M/remove" ] && echo 1 || echo 0)" >> "$S/supervisor.log"
  if [ "$failures" -ge 3 ] && [ -d "$M" ] && [ ! -e "$M/disable" ]; then
   printf '%s MONITOR_DISABLE_ATTEMPT monitor=%s rc=%s failures=%s reason=controller_failures\n' "$(date +%s)" "$$" "$rc" "$failures" >> "$S/supervisor.log"
   if touch "$M/disable"; then
    printf '%s MONITOR_DISABLE_WRITTEN monitor=%s rc=%s failures=%s reason=controller_failures\n' "$(date +%s)" "$$" "$rc" "$failures" >> "$S/supervisor.log"
   else
    printf '%s MONITOR_DISABLE_WRITE_FAILED monitor=%s rc=%s failures=%s reason=controller_failures\n' "$(date +%s)" "$$" "$rc" "$failures" >> "$S/supervisor.log"
    exit 1
   fi
  fi
 else failures=0; fi
 sleep 2
done
