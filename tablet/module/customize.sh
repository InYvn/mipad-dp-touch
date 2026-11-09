#!/system/bin/sh
# Manager installer: read-only environment checks plus module-local permissions.
[ "${KSU:-}" = true ] || abort "KernelSU only; Magisk execution domain is not supported."
[ "$(id -Z)" = u:r:ksu:s0 ] || abort "Installer must run in the verified KernelSU domain."
[ "$(id -u)" = 0 ] && [ "$(getenforce)" = Enforcing ] || abort "Root and enforcing profile required."
[ "$ARCH" = arm64 ] && [ "$(getprop ro.product.cpu.abi)" = arm64-v8a ] || abort "Android arm64 only."
sdk=$(getprop ro.build.version.sdk)
case "$sdk" in ''|*[!0-9]*) abort "Unknown Android API level.";; esac
[ "$sdk" -ge 35 ] || abort "Android API 35+ required."
[ -x /data/adb/ksu/bin/busybox ] || abort "KernelSU BusyBox runtime required."
# The manager extracts into modules_update and copies module.prop into modules
# only AFTER customize returns. A retained runtime can outlive the modules entry.
# Authenticate the exact incoming manager slot, never an arbitrary directory.
EXPECTED_ID=mipad_dptouch_fullsplit_local
case "$MODPATH" in
 /data/adb/modules_update/$EXPECTED_ID|/data/adb/modules/$EXPECTED_ID) ;;
 *) abort "Unknown incoming module location.";;
esac
[ ! -L "${MODPATH%/*}" ] && [ ! -L "$MODPATH" ] || abort "Unsafe incoming module path."
[ "${MODID:-$EXPECTED_ID}" = "$EXPECTED_ID" ] || abort "Unknown incoming module identity."
module_identity_ok() {
 [ -f "$1/module.prop" ] && [ ! -L "$1" ] && [ ! -L "$1/module.prop" ] || return 1
 # One anchored id only; accept CRLF and surrounding whitespace, not duplicate,
 # substring keys, shell evaluation, or getprop's unrelated system fallback.
 awk -v expected="$EXPECTED_ID" '
  { sub(/\r$/, ""); line=$0; sub(/^[ \t]+/, "", line)
    if (line ~ /^id[ \t]*=/) {
      sub(/^id[ \t]*=[ \t]*/, "", line); sub(/[ \t]+$/, "", line)
      count++; if (line != expected) bad=1
    }
  }
  END { exit !(count == 1 && !bad) }
 ' "$1/module.prop"
}
module_identity_ok "$MODPATH" || abort "Unknown incoming module identity."
(cd "$MODPATH" && sha256sum -c files.sha256) || abort "Package integrity check failed."
. "$MODPATH/profile.sh"
profile_check || abort "Unknown hardware/HAL/init contract. No activation permitted."
[ ! -d /data/adb/modules/mipad_dptouch ] || abort "Old SVID module exists. Controlled migration required; no foreign mount adoption."
# Identical-runtime reinstall is non-destructive: never copy, stop or retire live
# runtime helpers/owners from the manager installer. Different generations remain
# fail-closed; an ordinary uninstall is NOT a migration or root recovery method.
S=/data/adb/mipad_fullsplit_local
[ ! -L "$S" ] || abort "Unsafe runtime path."
if [ -e "$S" ] && [ ! -f "$S/runtime-ready" ]; then
 abort "Incomplete existing runtime: retain recovery helpers; controlled recovery required."
fi
if [ -e "$S/runtime-ready" ]; then
 [ -d "$S" ] && [ ! -L "$S" ] || abort "Unsafe runtime path."
 [ ! -L "$S/runtime-ready" ] || abort "Unsafe runtime ready marker."
 OLD_MODULE=/data/adb/modules/$EXPECTED_ID
 [ ! -L /data/adb/modules ] || abort "Unsafe existing modules path."
 if [ -e "$OLD_MODULE" ] || [ -L "$OLD_MODULE" ]; then
  module_identity_ok "$OLD_MODULE" || abort "Unknown existing module identity."
 fi
 # Missing installed metadata is not identity evidence: acceptance below still
 # requires the exact authenticated incoming generation and every runtime byte.
 [ -f "$S/runtime.sha256" ] && [ ! -L "$S/runtime.sha256" ] || abort "Unsafe or missing runtime manifest."
 cmp -s "$MODPATH/runtime.sha256" "$S/runtime.sha256" || abort "Different runtime generation: controlled migration required. Do not uninstall or reboot to bypass this guard."
 while read -r digest name; do
  [ -f "$S/$name" ] && [ ! -L "$S/$name" ] || abort "Unsafe or missing runtime helper."
 done < "$MODPATH/runtime.sha256"
 (cd "$S" && sha256sum -c "$MODPATH/runtime.sha256") || abort "Existing runtime integrity failed; retained owners untouched."
 ui_print "- 已验证现有运行时，保留运行状态并更新模块。"
fi
[ "$(readlink /proc/self/ns/mnt)" = "$(readlink /proc/1/ns/mnt)" ] || abort "Installer must see init mount namespace."
(cd "$MODPATH" && sha256sum -c files.sha256) || abort "Package integrity check failed."
set_perm_recursive "$MODPATH" 0 0 0755 0644
for f in module-controller platform-tool prepare-owner platform.sh bootstrap.sh runtime-monitor.sh service.sh post-fs-data.sh uninstall.sh action.sh; do set_perm "$MODPATH/$f" 0 0 0755; done
ui_print "- Mipad DP Touch 触控解锁 2.0.0"
ui_print "- 提供 DP-in 触摸屏与笔接口，并优化双指缩放。"
ui_print "- 仅支持 KernelSU / root / Enforcing / arm64。"
ui_print "- 需要匹配的小米平板 9 Pro Max（yingtian）硬件与 HAL/init 固件。"
ui_print "- 安装过程不启动触控服务；现有运行状态保持不变。"
