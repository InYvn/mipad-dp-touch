#!/system/bin/sh
# 升级自 2.2.1（接管 HAL 的版本）：清掉旧的运行时目录，它属于旧控制器，这里不再使用。
rm -rf /data/adb/mipad_fullsplit_local
set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/bin/hal-patch" 0 0 0755

# 契约校验失败时把设备实际值写进安装日志，再拒绝安装（只读 prop/hash，不做任何修改）。
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
