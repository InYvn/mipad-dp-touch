#!/bin/sh
# dpin-recover-host.sh — 在 Mac 上通过 adb 调用设备端 dpin-recover.sh / dpin-guard.sh。
# 只做"推文件 + 执行 + 拉证据"；所有判定都在设备端完成（见 dpin-recover.sh 注释）。
#
# 用法:
#   SERIAL=<序列号> ./dpin-recover-host.sh status
#   ./dpin-recover-host.sh check-chain --enter
#   ./dpin-recover-host.sh push
#   ./dpin-recover-host.sh install [--start]
#   ./dpin-recover-host.sh recover --yes
#   ./dpin-recover-host.sh framework-restart --dry-run
#   ./dpin-recover-host.sh snapshot
#   ./dpin-recover-host.sh logs
#   ./dpin-recover-host.sh uninstall
set -u

FIX=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)
ADB=${ADB:-adb}
SERIAL=${SERIAL:-}
DEV_MOD=/data/adb/modules/mipad_dpin_guard
DEV_RECOVER=$DEV_MOD/bin/dpin-recover.sh
DEV_STATE=/data/adb/mipad_dpin_guard

adb_() {
	if [ -n "$SERIAL" ]; then
		"$ADB" -s "$SERIAL" "$@"
	else
		"$ADB" "$@"
	fi
}

quote_sh() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# dev_su <word...> → adb shell su -c '<words joined>'
dev_su() {
	cmd=""
	for a in "$@"; do
		case $a in
		*[!A-Za-z0-9_./:=+-]*) cmd="$cmd $(quote_sh "$a")" ;;
		*) cmd="$cmd $a" ;;
		esac
	done
	cmd=${cmd# }
	adb_ shell "su -c $(quote_sh "$cmd")"
}

need_adb() {
	if ! command -v "$ADB" >/dev/null 2>&1; then
		echo "ERROR: 找不到 adb（可设 ADB=/path/to/adb）" >&2
		exit 127
	fi
}

push_all() {
	ts=$(date +%Y%m%d-%H%M%S)
	src=/data/local/tmp/mipad_dpin_guard_src-$ts
	echo "== push 模块到 $src" >&2
	adb_ shell "mkdir -p $src" || return 1
	adb_ push "$FIX/module/." "$src/" >&2 || return 1
	echo "== push 安装/卸载脚本到 /data/local/tmp" >&2
	adb_ push "$FIX/install/install-guard.sh" /data/local/tmp/install-guard.sh >&2 || return 1
	adb_ push "$FIX/install/uninstall-guard.sh" /data/local/tmp/uninstall-guard.sh >&2 || return 1
	printf '%s\n' "$src" >"$FIX/evidence/.last-pushed-src" 2>/dev/null || true
	printf '%s\n' "$src"
}

cmd=${1:-status}
[ $# -gt 0 ] && shift

case $cmd in
push)
	need_adb
	push_all
	;;
install)
	need_adb
	startarg=""
	for a in "$@"; do
		case $a in --start) startarg=--start ;; esac
	done
	src=""
	if [ -f "$FIX/evidence/.last-pushed-src" ]; then
		src=$(cat "$FIX/evidence/.last-pushed-src")
	fi
	if [ -z "$src" ]; then
		echo "== 先 push"
		src=$(push_all | tail -n 1)
	fi
	if [ -z "$src" ]; then echo "ERROR: push 失败" >&2; exit 1; fi
	echo "== install ($src)"
	dev_su sh /data/local/tmp/install-guard.sh "$src" $startarg
	;;
uninstall)
	need_adb
	dev_su sh /data/local/tmp/uninstall-guard.sh
	;;
status | check-chain | enter | exit | unlock | recover | framework-restart | allow-restart | selftest | version)
	need_adb
	dev_su sh "$DEV_RECOVER" "$cmd" "$@"
	;;
snapshot)
	need_adb
	out=$(dev_su sh "$DEV_RECOVER" snapshot "$@") || {
		echo "ERROR: 设备端 snapshot 失败" >&2
		exit 1
	}
	printf '%s\n' "$out"
	remote=$(printf '%s\n' "$out" | sed -n 's/^SNAPSHOT=//p' | tail -n 1)
	if [ -z "$remote" ]; then echo "ERROR: 未解析到 SNAPSHOT 路径" >&2; exit 1; fi
	localdir="$FIX/evidence/host-$(basename "$remote")"
	mkdir -p "$localdir"
	adb_ pull "$remote/." "$localdir/" || {
		echo "ERROR: adb pull 失败（$remote）" >&2
		exit 1
	}
	echo "本地证据: $localdir"
	;;
logs)
	need_adb
	dev_su sh -c "tail -n 200 $DEV_STATE/events.log; echo ---; tail -n 50 $DEV_STATE/hal-events.log; echo ---; tail -n 20 $DEV_STATE/verify.log"
	;;
guard)
	need_adb
	dev_su sh "$DEV_MOD/bin/dpin-guard.sh" "$@"
	;;
help | -h | --help)
	sed -n '2,20p' "$0"
	;;
*)
	echo "unknown command: $cmd" >&2
	sed -n '2,20p' "$0" >&2
	exit 2
	;;
esac
