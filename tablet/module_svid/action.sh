#!/system/bin/sh
# 立即应用：SVID + interposer（HAL/DSO bind）。
# 不重启、不主动拉起 HAL；HAL 已在跑就只布置 bind，并明确提示需要拔插。
set -u
MODDIR=${0%/*}
export MIPAD_PLATFORM_BUSYBOX=/data/adb/ksu/bin/busybox
[ -x "$MIPAD_PLATFORM_BUSYBOX" ] || exit 70
if [ "$(readlink /proc/self/ns/mnt)" != "$(readlink /proc/1/ns/mnt)" ]; then
 [ "${MIPAD_NS_ENTERED:-0}" = 0 ] || exit 70
 export MIPAD_NS_ENTERED=1
 exec "$MIPAD_PLATFORM_BUSYBOX" nsenter --mount=/proc/1/ns/mnt -- "$MIPAD_PLATFORM_BUSYBOX" sh -o standalone "$0" "$@"
fi
. "$MODDIR/profile.sh"
. "$MODDIR/common.sh"
. "$MODDIR/interposer.sh"

echo "== Mipad DP Touch 立即应用 =="
if profile_check; then
 echo "契约: ok"
else
 echo "契约: 未审计的硬件/HAL/init（只挂 SVID，不注入）"
fi

if run_locked enable; then
 echo "SVID: 已挂载"
else
 echo "SVID: 未挂载（节点不存在或结构不符）"
fi

interposer_reconcile
rc=$?
case "$rc" in
 0) echo "interposer: 已生效" ;;
 2) echo "interposer: 已布置，等待 HAL 自然重载（拔插 DP 线）" ;;
 3) echo "interposer: 原厂库正被进程映射，无法热应用（拔线等 HAL 退出，或重启）" ;;
 *) echo "interposer: 布置失败（见上面的日志）" ;;
esac
echo "--- 状态 ---"
interposer_report
[ "$rc" = 1 ] && exit 1
exit 0
