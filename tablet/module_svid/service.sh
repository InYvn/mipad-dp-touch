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

. "$MODDIR/interposer.sh"
interposer_reconcile
rc=$?
case "$rc" in
 0) echo "interposer: 已生效" ;;
 2) echo "interposer: 已布置，等待 HAL 自然重载（拔插 DP）" ;;
 3) echo "interposer: 原厂库正被映射，等 HAL 退出后再应用" ;;
 *) echo "interposer: 布置失败"; exit 1 ;;
esac
exit 0
