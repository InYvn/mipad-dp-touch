#!/system/bin/sh
# Fingerprint is provenance, not a gate: same hardware + exact HAL and init contract
# may survive firmware upgrades; changed HAL/init must be audited as a new profile.
profile_check() {
 [ "$(getprop ro.product.device)" = yingtian ] &&
 [ "$(getprop ro.product.model)" = M367FC ] &&
 [ "$(getprop ro.product.board)" = O3 ] &&
 [ "$(getprop ro.hardware)" = xring_o3_asic ] || return 1
 [ "$(sha256sum /odm/bin/vendor.xiaomi.usb.dp-service | cut -d ' ' -f1)" = 44efbe22de46196246c9dbf96bceba8444e5f53aea2400b8101b1b0b0d79c5c3 ] || return 1
 [ "$(sha256sum /odm/etc/init/vendor.xiaomi.usb.dp-service.rc | cut -d ' ' -f1)" = 24c8b3714ae98e813017b8944483fafdb2fa398cc46003efac8c3295480dcd0a ] || return 1
}
