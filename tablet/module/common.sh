#!/system/bin/sh
# Mipad DP Touch 触控解锁 —— 共用函数（post-fs-data / service / action 都 source 这个）
#
# 背景（2026-10 反汇编 + 实测钉死）：
#   DP-in 时官方 /odm/bin/vendor.xiaomi.usb.dp-service 会读
#       /sys/devices/platform/mca_strategy_pd_auth/adapter_svid
#   读到 1452（= 0x5AC = Apple VID）-> 认为主机是 Mac -> 把 hid.usb1 写成 107 字节「只笔」描述符；
#   读到别的值（或读失败）    -> 认为主机不是 Mac -> 写 914 字节「触摸屏 + 笔」描述符。
#   Mac 端拿到的就是这条 hid.usb1，所以：把那个节点盖住 = 让 Mac 也能拿到触摸屏接口。
#
# 只动内存：假文件放在 /data/adb/mipad_dptouch/，用 bind mount 盖在 sysfs 节点上；
#           卸载 / 重启即复原，不刷分区、不改 /odm、不 remount、不碰任何系统文件。

STATE=/data/adb/mipad_dptouch
TARGET=/sys/devices/platform/mca_strategy_pd_auth/adapter_svid
FAKE=$STATE/fake_svid
LOG=$STATE/log.txt

mkdir -p "$STATE" 2>/dev/null          # 先建好状态目录: 下面 dlog 要往里写

dlog() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null; }

# 节点在不在（不在 = 不是这台机型，什么都不做）
have_target() { [ -e "$TARGET" ]; }

# 已经挂上了没？HAL 在 init 的 mount namespace 里，两边都看一眼
is_mounted() {
    grep -q " $TARGET " /proc/mounts 2>/dev/null && return 0
    nsenter --mount=/proc/1/ns/mnt -- grep -q " $TARGET " /proc/mounts 2>/dev/null && return 0
    return 1
}

apply() {
    if [ -f "$STATE/off" ]; then
        dlog "已停用（存在 $STATE/off）"
        return 0
    fi
    if ! have_target; then
        dlog "没有 $TARGET —— 不是目标机型，什么都不做"
        return 0
    fi
    mkdir -p "$STATE" 2>/dev/null
    printf '0' > "$FAKE" || { dlog "写假文件失败"; return 1; }
    chmod 644 "$FAKE"
    if is_mounted; then
        dlog "已经挂着，跳过"
        return 0
    fi
    # 关键：挂进 init 的 namespace —— HAL 进程在那边，挂在自己这边它读到的还是真值。
    # （有的机型 adb/模块脚本本来就在全局 namespace，nsenter 失败就退回直接挂。）
    if nsenter --mount=/proc/1/ns/mnt -- mount --bind "$FAKE" "$TARGET" 2>>"$LOG"; then
        dlog "已挂载（init 命名空间）: $FAKE -> $TARGET"
        return 0
    fi
    if mount --bind "$FAKE" "$TARGET" 2>>"$LOG"; then
        dlog "已挂载（当前命名空间）: $FAKE -> $TARGET"
        return 0
    fi
    dlog "挂载失败"
    return 1
}

remove() {
    if nsenter --mount=/proc/1/ns/mnt -- umount "$TARGET" 2>/dev/null; then
        dlog "已卸载（init 命名空间）"
        return 0
    fi
    if umount "$TARGET" 2>/dev/null; then
        dlog "已卸载"
        return 0
    fi
    dlog "卸载失败（可能本来就没挂）"
    return 1
}
