#!/system/bin/sh
# 两件事，都在开机时做完：
#   1) 把 adapter_svid 内存 bind-mount 成 0，让厂商 HAL 认为对端是 Mac；
#   2) 给厂商 HAL 注入 interposer（双指抬手修正），不改磁盘、不接管 HAL：
#      - 在 /mnt/vendor 建一个非 nosuid 的 tmpfs；
#      - 用自带的 hal-patch 把原厂 HAL 复制一份并加一条 DT_NEEDED；
#      - 把 DSO 和 patched HAL 分别 bind 到 /odm/lib64 与 /odm/bin。
# HAL 仍由 init/系统客户端启动，prepare 归属不变，平板本地触控保留。
set -eu
umask 077
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
[ "$(id -u)" = 0 ] && [ "$(id -Z)" = u:r:ksu:s0 ] || exit 70
[ "$(getenforce)" = Enforcing ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
. "$MODDIR/profile.sh"
# 契约失败时留一行可排查的记录（只读 prop/hash），再按原语义退出。
diag_fail() {
 S=/data/adb/mipad_dptouch_v102
 mkdir -p "$S" 2>/dev/null || true
 {
  echo "time=$(date)"
  echo "device=$(getprop ro.product.device) model=$(getprop ro.product.model) board=$(getprop ro.product.board) hw=$(getprop ro.hardware)"
  echo "hal=$(sha256sum /odm/bin/vendor.xiaomi.usb.dp-service 2>/dev/null | cut -d ' ' -f1)"
  echo "rc=$(sha256sum /odm/etc/init/vendor.xiaomi.usb.dp-service.rc 2>/dev/null | cut -d ' ' -f1)"
 } >> "$S/last-error.log" 2>/dev/null || true
}
# 契约失败不再阻止 SVID（只影响注入），记一行便于排查。
profile_check || diag_fail
. "$MODDIR/common.sh"
n=0
while [ "$(getprop sys.boot_completed)" != 1 ] && [ "$n" -lt 60 ]; do sleep 1; n=$((n+1)); done
run_locked enable || exit 1

# ---------------------------------------------------------------- interposer
# 注入只对审计过的 HAL 做：未审计的二进制不 patch，SVID 已经生效。
profile_check || exit 0
HAL=/odm/bin/vendor.xiaomi.usb.dp-service
DSO_TARGET=/odm/lib64/libanc_supermoon.so
LIBDIR=/mnt/vendor/mipad_lib
PATCH="$MODDIR/bin/hal-patch"
DSO="$MODDIR/lib/fullsplit-v4.so"
HAL_SHA=$(sha256sum "$HAL" | cut -d ' ' -f1)
case "$HAL_SHA" in
 44efbe22de46196246c9dbf96bceba8444e5f53aea2400b8101b1b0b0d79c5c3|\
 17b70644c4cd1ed8421616704eb72641aad5b7c00d94ecf029d324ce02d5a922) ;;
 *) exit 0 ;;   # 未审计的 HAL：只保留 SVID，不注入
esac
[ -x "$PATCH" ] || exit 0
[ -f "$DSO" ] || exit 0

# 目标库必须存在且当前没有进程映射它，否则换一个也不能乱覆盖
if [ ! -e "$DSO_TARGET" ] || grep -q libanc_supermoon /proc/[0-9]*/maps 2>/dev/null; then
 exit 0
fi

mkdir -p "$LIBDIR"
mountpoint -q "$LIBDIR" || mount -t tmpfs -o size=16m tmpfs "$LIBDIR" || exit 0
cp "$DSO" "$LIBDIR/dso.so" && chmod 644 "$LIBDIR/dso.so" &&
 chcon u:object_r:vendor_file:s0 "$LIBDIR/dso.so" || exit 0

"$PATCH" "$HAL" "$LIBDIR/hal-patched" "$DSO_TARGET" || exit 0
chmod 755 "$LIBDIR/hal-patched"
chcon u:object_r:hal_usb_dp_relay_default_exec:s0 "$LIBDIR/hal-patched" || exit 0

# 先挂 DSO，再挂 HAL；任一失败就回滚已挂的部分
mount --bind "$LIBDIR/dso.so" "$DSO_TARGET" || exit 0
if ! mount --bind "$LIBDIR/hal-patched" "$HAL"; then
 umount "$DSO_TARGET" 2>/dev/null || true
 exit 0
fi
