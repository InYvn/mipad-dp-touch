#!/system/bin/sh
# =============================================================================
# mac-touch.sh —— 让小米平板在 DP-in 外接 Mac 时，把「手指触控」也传给主机
#
# 给【临时 root】用户用：提权之后跑一次 `on`，这次会话就能用手指导航。
# 永久 root 的用户请装同目录下的模块（tablet/module/，见 tablet/README.md），
# 它会在每次开机自动应用，不用手动跑。
#
# 背景（2026-10 反汇编 + 实测，见 docs/firmware-issue-apple-svid.md）：
#   平板的 usb_dp_relay 读 /sys/devices/platform/mca_strategy_pd_auth/adapter_svid，
#   只要这个值是 0x05AC(Apple) 就判 is_mac=1，于是 hid.usb1 只写 107 字节的「纯笔」
#   描述符；不是 Apple 时写的是 914 字节的「触摸+笔」二合一描述符（10 指 + 笔）。
#   本脚本不碰任何系统文件 —— 只在内存里用一个 bind-mount 把那个节点盖住，
#   让 HAL 读到「不是 Apple」，于是它自己就把触摸那一路建出来。
#
# 用法（需要 root；平板上跑，或用 adb shell su -c 跑）：
#   sh mac-touch.sh on        # 开启（之后拔插一次 USB-C 线 / 重进一次 DP-in 生效）
#   sh mac-touch.sh off       # 还原（正常显示/笔不受影响）
#   sh mac-touch.sh status    # 看当前状态
#
# 注意：
#   · 临时 root 是内存态：**重启后失效**，需要重新 root 再跑一次 on。
#   · 只改内存挂载，不写磁盘、不刷分区、不动 boot —— 最坏情况是 relay 不工作，
#     重进一次 DP-in 或重启即完全复原。
#   · Mac 侧需要 App「Mipad DP Touch」≥1.2；「增强模式」默认就是开的，不用设置。
# =============================================================================

NODE=/sys/devices/platform/mca_strategy_pd_auth/adapter_svid
STATE=/data/adb/mipad_dptouch          # 与模块共用同一份状态目录
FAKE=$STATE/fake_svid

die() { echo "错误: $*" >&2; exit 1; }

is_root() { [ "$(id -u 2>/dev/null)" = "0" ]; }

# HAL 在 init 的 mount namespace 里 —— 挂进那边它才看得见；失败就退回当前命名空间
is_mounted() {
  grep -q " $NODE " /proc/mounts 2>/dev/null && return 0
  nsenter --mount=/proc/1/ns/mnt -- grep -q " $NODE " /proc/mounts 2>/dev/null && return 0
  return 1
}

case "$1" in
  on)
    is_root || die "需要 root（先用临时 root 工具提权，再跑本脚本）"
    [ -e "$NODE" ] || die "找不到 $NODE —— 这台机器的 relay 可能不是这一版"
    if is_mounted; then
      echo "已经开着（$NODE 已挂上假值）"
      exit 0
    fi
    mkdir -p "$STATE" || die "建不了 $STATE"
    # 值取一个肯定不等于 0x05AC(1452) 的数；就算 HAL 因 SELinux 读不进来，
    # 它的读取失败分支同样落到「非 Mac」——两条路都得到同一个结果。
    printf 0 > "$FAKE" || die "写 $FAKE 失败"
    chmod 644 "$FAKE"
    if nsenter --mount=/proc/1/ns/mnt -- mount --bind "$FAKE" "$NODE" 2>/dev/null; then
      echo "已开启（init 命名空间）。"
    elif mount --bind "$FAKE" "$NODE"; then
      echo "已开启（当前命名空间）。"
    else
      die "bind-mount 失败（SELinux 挡住了？）"
    fi
    echo "现在请：拔插一次 USB-C 线（或重进一次 DP-in），"
    echo "HAL 就会按「非 Mac 主机」建出 914 字节的触摸+笔接口。"
    echo "Mac 侧 App 的「增强模式」保持默认开启即可。"
    echo
    echo "验证（平板侧）：logcat -d | grep usb_dp_relay  应看到："
    echo "    host type: Windows"
    echo "    hid.usb1 desc: 914 bytes"
    ;;
  off)
    is_root || die "需要 root"
    if is_mounted; then
      nsenter --mount=/proc/1/ns/mnt -- umount "$NODE" 2>/dev/null || umount "$NODE" || die "umount 失败"
      echo "已还原（重进一次 DP-in 后回到原来的纯笔接口）"
    else
      echo "本来就没开着"
    fi
    ;;
  status)
    if is_mounted; then
      echo "状态: 已开启（内存挂载中, 值=$(cat "$NODE" 2>/dev/null)）"
    else
      echo "状态: 未开启（HAL 会读到真实 SVID）"
    fi
    echo "当前 relay: $(logcat -d 2>/dev/null | grep -a usb_dp_relay | tail -1)"
    ;;
  *)
    echo "用法: sh $0 on|off|status"
    exit 1
    ;;
esac
