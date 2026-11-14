#!/system/bin/sh
# uninstall-guard.sh — 停掉守护并把模块目录移出 /data/adb/modules（不删除任何东西）。
# KernelSU 只在开机时加载模块，所以移出后下次开机即不再生效；若要保持"本 boot 立即失效"，
# 本脚本还会 stop 守护进程。state 目录保留供事后分析。
set -u
umask 077

MOD=${DPIN_MOD_DIR:-/data/adb/modules/mipad_dpin_guard}
STATE=${DPIN_STATE_ROOT:-/data/adb/mipad_dpin_guard}
if [ "$(id -u 2>/dev/null)" != "0" ]; then
	echo "ERROR: 需要 root（KernelSU）"
	exit 1
fi
if [ -r "$MOD/bin/dpin-guard.sh" ]; then
	sh "$MOD/bin/dpin-guard.sh" stop 2>/dev/null || true
fi
if [ ! -d "$MOD" ]; then
	echo "模块目录不存在，无需卸载: $MOD"
	exit 0
fi
dst="$STATE/removed-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$STATE" 2>/dev/null
if mv "$MOD" "$dst"; then
	echo "已停用并移出: $MOD → $dst"
	echo "恢复: mv $dst $MOD   （或重新运行 install-guard.sh）"
	echo "注: KernelSU 在下次开机前仍按本 boot 的加载状态运行；守护进程已停止，无其它残留动作。"
	exit 0
fi
echo "ERROR: 移出失败（$MOD）"
exit 1
