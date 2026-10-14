#!/system/bin/sh
# KernelSU / Magisk 管理器里的「运行」按钮：切换启用 / 停用，并把结果打出来。
MODDIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)   # 手跑也能用（$0 可能没斜杠）
. "$MODDIR/common.sh"

if is_mounted; then
    remove
    touch "$STATE/off"
    echo "已停用：触摸屏接口不再建给主机（拔插一次 USB-C 生效）"
    echo "想再打开，再点一次这个按钮。"
else
    rm -f "$STATE/off"
    if apply; then
        echo "已启用：主机将拿到「触摸屏 + 笔」接口（拔插一次 USB-C 生效）"
        echo "Mac 端 App 需 1.2 及以上（默认「增强模式」已开启，无需额外设置）。"
    else
        echo "启用失败，日志：$LOG"
    fi
fi
