#!/system/bin/sh
# 安装时打印说明（Magisk / KernelSU 的安装器都会跑这个）。只打印，不动文件。
uip() { if command -v ui_print >/dev/null 2>&1; then ui_print "$1"; else echo "$1"; fi; }

uip "- Mipad DP Touch 触控解锁"
uip "- 装好后：拔插一次 USB-C（或退出 DP-in 再进）"
uip "- Mac 端：Mipad DP Touch 1.2 及以上；「增强模式」默认已开，不用额外设置"
uip "- 想临时关掉：模块卡片上的「运行」按钮点一下；再点一次恢复"
uip "- 日志：/data/adb/mipad_dptouch/log.txt"
