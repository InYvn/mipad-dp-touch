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
# Fail-closed diagnostics: on contract rejection, print the observed device/HAL
# values into the installer log before aborting. This only reads props and hashes
# files; the accepted set is still profile_check's.
profile_diagnostics() {
 hal=/odm/bin/vendor.xiaomi.usb.dp-service
 rc=/odm/etc/init/vendor.xiaomi.usb.dp-service.rc
 ui_print "- 硬件/HAL/init 契约校验失败；以下为设备实际值（安装器未做任何修改）："
 ui_print "  ro.product.device = $(getprop ro.product.device)"
 ui_print "  ro.product.model  = $(getprop ro.product.model)"
 ui_print "  ro.product.board  = $(getprop ro.product.board)"
 ui_print "  ro.hardware       = $(getprop ro.hardware)"
 if [ -r "$hal" ]; then
  ui_print "  $hal sha256 = $(sha256sum "$hal" | cut -d ' ' -f1)"
 else
  ui_print "  $hal 不存在或不可读"
 fi
 if [ -r "$rc" ]; then
  ui_print "  $rc sha256 = $(sha256sum "$rc" | cut -d ' ' -f1)"
 else
  ui_print "  $rc 不存在或不可读"
 fi
 ui_print "  期望 device=yingtian model=M367FC board=O3 hardware=xring_o3_asic"
 ui_print "  期望 $rc sha256 = 24c8b3714ae98e813017b8944483fafdb2fa398cc46003efac8c3295480dcd0a"
 ui_print "  允许的 HAL sha256 = 44efbe22de46196246c9dbf96bceba8444e5f53aea2400b8101b1b0b0d79c5c3 (OS4.0.10)"
 ui_print "                      17b70644c4cd1ed8421616704eb72641aad5b7c00d94ecf029d324ce02d5a922 (OS4.0.16)"
}
. "$MODPATH/profile.sh"
profile_check || { profile_diagnostics; abort "Unknown hardware/HAL/init contract. No activation permitted."; }
[ ! -d /data/adb/modules/mipad_dptouch ] || abort "Old SVID module exists. Controlled migration required; no foreign mount adoption."
# Identical-runtime reinstall is non-destructive: never copy, stop or retire live
# runtime helpers/owners from the manager installer. A retained runtime from the
# published 2.0.0 generation is upgraded in place, but only when its manifest is
# the exact pinned bytes and nothing live depends on it; every other generation
# stays fail-closed, and an ordinary uninstall is NOT a migration method.
S=/data/adb/mipad_fullsplit_local
[ ! -L "$S" ] || abort "Unsafe runtime path."
PREV_RUNTIME_SHA256=f0282236188b796a0ca8c1ffb37d369f1809dff5b9a9bd18ac6d7e87e1935f7a
# 2.0.1 shipped this manifest; it differs from the current one only in profile.sh
# (the audited HAL allowlist), so it is a known predecessor worth migrating.
PREV_RUNTIME_SHA256_201=acb2c42bacf44409ae569eea3ce34f7186e72233b4bfb22c3212107d5a16a307
SVID_TARGET=/sys/devices/platform/mca_strategy_pd_auth/adapter_svid
MOUNTINFO=${MIPAD_TEST_MOUNTINFO:-/proc/self/mountinfo}
migrate_known_generation() {
 [ -f "$S/runtime.sha256" ] && [ ! -L "$S/runtime.sha256" ] || return 1
 _prev=$(sha256sum "$S/runtime.sha256" | cut -d' ' -f1)
 [ "$_prev" = "$PREV_RUNTIME_SHA256" ] || [ "$_prev" = "$PREV_RUNTIME_SHA256_201" ] || return 1
 (cd "$S" && sha256sum -c runtime.sha256) >/dev/null 2>&1 || return 1
 # A live hook owns the gadget and its journals; never migrate under it.
 for f in pending candidate candidate-owner; do
  [ ! -e "$S/$f" ] && [ ! -L "$S/$f" ] || return 1
 done
 [ -r "$MOUNTINFO" ] || return 1
 [ -z "$(awk -v t="$SVID_TARGET" '$5==t {print $1}' "$MOUNTINFO")" ] || return 1
 # Only differing files are replaced, so an inode a live process still holds
 # (the retained prepare owner) keeps its path and its journal stays valid.
 # A generation that changes prepare-owner while such an owner is live needs
 # the operator-controlled migration instead.
 while read -r digest name; do
  old=$(awk -v n="$name" '$2==n {print $1}' "$S/runtime.sha256")
  [ "$old" = "$digest" ] && continue
  if [ "$name" = prepare-owner ] && { [ -e "$S/recovery-owner" ] || [ -e "$S/recovery-owner-replacement" ]; }; then
   return 1
  fi
  src="$MODPATH/$name"
  [ -f "$src" ] && [ ! -L "$src" ] || return 1
  cp "$src" "$S/$name.migrate" || return 1
  [ "$(sha256sum "$S/$name.migrate" | cut -d' ' -f1)" = "$digest" ] || return 1
  if [ "$(id -u)" = 0 ]; then chown 0:0 "$S/$name.migrate" || return 1; fi
  chmod 700 "$S/$name.migrate" || return 1
 done < "$MODPATH/runtime.sha256"
 [ ! -f "$S/fullsplit-bounded-v2.so.migrate" ] || chmod 600 "$S/fullsplit-bounded-v2.so.migrate" || return 1
 while read -r digest name; do
  [ -f "$S/$name.migrate" ] || continue
  mv "$S/$name.migrate" "$S/$name" || return 1
 done < "$MODPATH/runtime.sha256"
 cp "$MODPATH/runtime.sha256" "$S/runtime.sha256.migrate" || return 1
 if [ "$(id -u)" = 0 ]; then chown 0:0 "$S/runtime.sha256.migrate" || return 1; fi
 chmod 600 "$S/runtime.sha256.migrate" || return 1
 mv "$S/runtime.sha256.migrate" "$S/runtime.sha256" || return 1
 (cd "$S" && sha256sum -c runtime.sha256) >/dev/null 2>&1 || return 1
 ui_print "- 已把保留运行时从 2.0.0 受控升级到当前版本。"
}
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
 if ! cmp -s "$MODPATH/runtime.sha256" "$S/runtime.sha256"; then
  migrate_known_generation || abort "Different runtime generation: controlled migration required. Do not uninstall or reboot to bypass this guard."
 fi
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
ui_print "- Mipad DP Touch 触控解锁 2.0.1"
ui_print "- 提供 DP-in 触摸屏与笔接口，并优化双指缩放。"
ui_print "- 仅支持 KernelSU / root / Enforcing / arm64。"
ui_print "- 需要匹配的小米平板 9 Pro Max（yingtian）硬件与 HAL/init 固件。"
ui_print "- 安装过程不启动触控服务；现有运行状态保持不变。"
