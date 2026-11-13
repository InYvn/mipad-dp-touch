#!/system/bin/sh
# 升级自 2.2.1（接管 HAL 的版本）：清掉旧的运行时目录，它属于旧控制器，这里不再使用。
rm -rf /data/adb/mipad_fullsplit_local
set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/bin/hal-patch" 0 0 0755
