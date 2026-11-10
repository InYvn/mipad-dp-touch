#!/system/bin/sh
# Fingerprint is provenance, not a gate: same hardware + exact HAL and init contract
# may survive firmware upgrades; changed HAL/init must be audited as a new profile.
# Audited HAL profiles:
#   44efbe22... 102552 B  OS4.0.10.0.XBMCNXM
#   17b70644... 102568 B  OS4.0.16.0.XBMCNXM (+16 B: adds inject_local_touch_up only;
#                         init rc, AIDL lifecycle and HID descriptors byte-identical)
profile_check() {
 [ "$(getprop ro.product.device)" = yingtian ] &&
 [ "$(getprop ro.product.model)" = M367FC ] &&
 [ "$(getprop ro.product.board)" = O3 ] &&
 [ "$(getprop ro.hardware)" = xring_o3_asic ] || return 1
 hal_sha=$(sha256sum /odm/bin/vendor.xiaomi.usb.dp-service | cut -d ' ' -f1) || return 1
 case "$hal_sha" in
 44efbe22de46196246c9dbf96bceba8444e5f53aea2400b8101b1b0b0d79c5c3|\
 17b70644c4cd1ed8421616704eb72641aad5b7c00d94ecf029d324ce02d5a922) ;;
 *) return 1 ;;
 esac
 [ "$(sha256sum /odm/etc/init/vendor.xiaomi.usb.dp-service.rc | cut -d ' ' -f1)" = 24c8b3714ae98e813017b8944483fafdb2fa398cc46003efac8c3295480dcd0a ] || return 1
}
