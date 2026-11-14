#!/system/bin/sh
# install-guard.sh — 在设备上（root）把本目录安装为 KernelSU 模块 mipad_dpin_guard。
# 只写 /data/adb/modules/mipad_dpin_guard 与 /data/adb/mipad_dpin_guard；
# 不碰 /odm、不 reload/修改触摸模块、不重启任何服务。
#
# 用法（设备端）:
#   adb push tablet/module_guard/module /data/local/tmp/mipad_dpin_guard_src
#   adb shell su -c 'sh /data/local/tmp/install-guard.sh /data/local/tmp/mipad_dpin_guard_src'
#   adb shell su -c 'sh /data/local/tmp/install-guard.sh /data/local/tmp/mipad_dpin_guard_src --start'
set -u
umask 077

MOD=${DPIN_MOD_DIR:-/data/adb/modules/mipad_dpin_guard}
STATE=${DPIN_STATE_ROOT:-/data/adb/mipad_dpin_guard}
src=""
start=0
for a in "$@"; do
	case $a in
	--start) start=1 ;;
	*) src=$a ;;
	esac
done
[ -n "$src" ] || src=/data/local/tmp/mipad_dpin_guard_src

if [ "$(id -u 2>/dev/null)" != "0" ]; then
	echo "ERROR: 需要 root（KernelSU）"
	exit 1
fi
if [ ! -f "$src/module.prop" ]; then
	echo "ERROR: 找不到 $src/module.prop（先把 module 目录 push 上来）"
	exit 1
fi
mid=$(awk -F= '$1=="id"{print $2; exit}' "$src/module.prop")
if [ "$mid" != "mipad_dpin_guard" ]; then
	echo "ERROR: module.prop id='$mid'，期望 mipad_dpin_guard"
	exit 1
fi

mkdir -p "$STATE" || exit 1
if [ -d "$MOD" ]; then
	bak="$STATE/backup-$(date +%Y%m%d-%H%M%S)"
	if mv "$MOD" "$bak"; then
		echo "旧模块目录已备份: $bak"
	else
		echo "ERROR: 备份旧模块失败，停止"
		exit 1
	fi
fi
mkdir -p "$(dirname "$MOD")" || exit 1
if ! cp -R "$src" "$MOD"; then
	echo "ERROR: 复制模块失败"
	exit 1
fi
chmod 755 "$MOD/post-fs-data.sh" "$MOD/service.sh" "$MOD/uninstall.sh" 2>/dev/null
chmod 755 "$MOD/bin/"*.sh 2>/dev/null
chmod 644 "$MOD/module.prop" 2>/dev/null
mkdir -p "$STATE/state" 2>/dev/null
if [ ! -f "$STATE/config" ] && [ -f "$MOD/config/config" ]; then
	cp "$MOD/config/config" "$STATE/config" 2>/dev/null || true
fi
# 安全：重装/升级不继承"框架重启"这个危险开关（fail-closed），旧 config 留档
if [ -f "$STATE/config" ] && grep -q '^ALLOW_FRAMEWORK_RESTART=1' "$STATE/config" 2>/dev/null; then
	cp "$STATE/config" "$STATE/config.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
	sed 's/^ALLOW_FRAMEWORK_RESTART=1/ALLOW_FRAMEWORK_RESTART=0/' "$STATE/config" >"$STATE/config.tmp" &&
		mv "$STATE/config.tmp" "$STATE/config" || true
	echo "安全：重装已把 ALLOW_FRAMEWORK_RESTART 复位为 0（旧 config 留档 config.bak-*）"
fi

rc=0
sh "$MOD/bin/dpin-guard.sh" selftest || rc=$?
echo "模块已安装: $MOD"
echo "状态目录  : $STATE（config: $STATE/config，事件: $STATE/events.log）"
if [ "$start" = "1" ]; then
	sh "$MOD/bin/dpin-guard.sh" start || rc=$?
else
	echo "未启动守护（next boot 由 service.sh 自动启动）。立即启动: sh $MOD/bin/dpin-guard.sh start"
fi
echo "回退: sh /data/local/tmp/uninstall-guard.sh   # 或 mv $STATE/removed-* 回 $MOD"
exit "$rc"
