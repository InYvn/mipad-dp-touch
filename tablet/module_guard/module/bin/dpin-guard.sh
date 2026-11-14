#!/system/bin/sh
# dpin-guard.sh — DP-in 回调守卫守护进程（KernelSU 模块 mipad_dpin_guard 的主程序）
#
# 根因（B2 evidence/PIPELINE-TRACE.md）：DfMS.DisplayFeatureManagerService 只在
# onBootPhase(PHASE_BOOT_COMPLETED=500) 注册一次 HAL 回调；若该时刻 displayfeature HAL 的
# AIDL 服务尚未发布，initWrapperLocked() 静默 return，之后不会再注册。HAL 死亡后也只把
# mWrapper=null，不重建、不重注册。后果：HAL 收到 DP-IN status=0 却广播给 0 个消费者，
# App 无 displayParam → 有背光全黑。
#
# 本进程做的事：盯住 system_server 的每次启动，在 DfMS 进入 phase 500 之前确保
# displayfeature HAL 已经起来并在 servicemanager 可见（`setprop ctl.start`，幂等）。
# 它不修改 framework、不 hook、不写 /odm、不碰 DP/UDC/i2c/sysfs、不动触摸模块。
#
# 用法：
#   dpin-guard.sh start            启动守护（幂等）
#   dpin-guard.sh daemon           前台运行（供 init/service.sh 使用）
#   dpin-guard.sh stop             停止守护
#   dpin-guard.sh status           一次性状态 + 链路判定 + 修复建议
#   dpin-guard.sh verify           立即做一次注册证据核查
#   dpin-guard.sh boot-report      记录 boot 时序快照
#   dpin-guard.sh selftest         配置/依赖自检（离线可用）
set -u

SELF=$0
case $SELF in
*/*) HERE=${SELF%/*} ;;
*) HERE=. ;;
esac
case $SELF in
/*) : ;;
*) SELF=$(cd "$HERE" 2>/dev/null && pwd)/${SELF##*/} ;;
esac
. "$HERE/dpin-lib.sh"

PASS=0
FAIL=1
UNKNOWN=2

dpin_daemon_running() {
	pf="$DPIN_STATE/daemon.pid"
	[ -r "$pf" ] || return 1
	p=$(awk 'NR==1{print $1}' "$pf" 2>/dev/null)
	case ${p:-} in '' | *[!0-9]*) return 1 ;; esac
	[ "$p" = "0" ] && return 1
	kill -0 "$p" 2>/dev/null || return 1
	printf '%s\n' "$p"
	return 0
}

dpin_write_pid() {
	printf '%s\n' "$1" >"$DPIN_STATE/daemon.pid" 2>/dev/null || true
}

# ---------------------------------------------------------------- 主循环

dpin_daemon() {
	dpin_state_init
	dpin_load_config
	if [ "$ENABLE" != "1" ]; then
		dpin_log "guard 未启用（ENABLE=$ENABLE），退出"
		return 0
	fi
	if p=$(dpin_daemon_running); then
		dpin_log "guard 已在运行 pid=$p，本实例退出"
		return 0
	fi
	dpin_write_pid "$$"
	dpin_log "guard 启动 pid=$$ POLL=${POLL}s HAL_WAIT=${HAL_WAIT}s PHASE500_GUARD=${PHASE500_GUARD}s VERIFY_DELAY=${VERIFY_DELAY}s"

	dpin_attempts_boot=0
	dpin_attempts_fw=0
	dpin_last_start=0
	last_ss=""
	ss_start=0
	last_hal=""
	verify_done_for=""
	loops=0
	maxloops=${DPIN_DAEMON_MAX_LOOPS:-0}

	while :; do
		loops=$((loops + 1))

		ss=$(dpin_ss_pid 2>/dev/null || true)
		if [ -n "$ss" ] && [ "$ss" != "$last_ss" ]; then
			# 新的 framework 世代：phase 500 的注册窗口即将打开
			last_ss=$ss
			dpin_attempts_fw=0
			verify_done_for=""
			ss_start=$(dpin_pid_start_epoch "$ss" 2>/dev/null || echo 0)
			dpin_log "检测到 system_server 启动 pid=$ss start_epoch=$ss_start → 抢在 DfMS phase500 前确保 HAL 可用"
			dpin_ensure_hal "framework-start" "$ss_start" || true
		fi

		hal=$(dpin_hal_pid 2>/dev/null || true)
		if [ -n "$hal" ] && [ "$hal" != "$last_hal" ] && [ -n "$last_hal" ] && [ "$ss_start" -gt 0 ] &&
			[ "$(dpin_pid_start_epoch "$hal" 2>/dev/null || echo 0)" -ge "$ss_start" ]; then
			# HAL 在 framework 启动之后重启过：DfMS 的 death handler 只置 mWrapper=null，不会重注册
			dpin_log "WARN: HAL 在 framework 启动后重启（old=$last_hal new=$hal）→ 本世代注册很可能已丢失；用 dpin-recover.sh recover --yes 修复"
			dpin_write_hal_event "hal-restarted-after-framework" "old=$last_hal new=$hal" "watch"
		fi
		[ -n "$hal" ] && last_hal=$hal
		if [ -z "$hal" ]; then
			dpin_ensure_hal "hal-missing" "$ss_start" || true
		fi

		if [ -n "$last_ss" ] && [ "$verify_done_for" != "$last_ss" ] && [ "$ss_start" -gt 0 ]; then
			now=$(dpin_ts)
			if [ $((now - ss_start)) -ge "$VERIFY_DELAY" ]; then
				dpin_verify_registration "$last_ss" || true
				verify_done_for=$last_ss
			fi
		fi

		if [ "$maxloops" != "0" ] && [ "$loops" -ge "$maxloops" ]; then
			dpin_log "达到 DPIN_DAEMON_MAX_LOOPS=$maxloops，退出（测试用）"
			break
		fi
		sleep "$POLL" 2>/dev/null || sleep 2
	done
	dpin_write_pid "0"
	return 0
}

dpin_start() {
	dpin_state_init
	dpin_load_config
	if p=$(dpin_daemon_running); then
		dpin_log "guard 已在运行 pid=$p"
		return 0
	fi
	if [ "$ENABLE" != "1" ]; then
		dpin_log "guard 未启用（ENABLE=$ENABLE），不启动"
		return 0
	fi
	dpin_boot_report || true
	# 脱离调用者的终端：忽略 HUP、断开 stdin，避免 adb shell 退出时被带走
	( trap '' HUP; exec sh "$SELF" daemon ) </dev/null >>"$DPIN_STATE/daemon.log" 2>&1 &
	child=$!
	sleep 0.3 2>/dev/null || true
	if p=$(dpin_daemon_running); then
		dpin_log "guard 已启动 pid=$p（日志 $DPIN_STATE/daemon.log）"
		return 0
	fi
	if kill -0 "$child" 2>/dev/null; then
		dpin_log "guard 子进程 pid=$child 运行中，但尚未写入 pid 文件"
		return 0
	fi
	dpin_log "ERROR: guard 启动失败，见 $DPIN_STATE/daemon.log"
	return 1
}

dpin_stop() {
	pf="$DPIN_STATE/daemon.pid"
	p=$(dpin_daemon_running 2>/dev/null || true)
	if [ -z "${p:-}" ]; then
		dpin_log "guard 未在运行"
		dpin_write_pid "0"
		return 0
	fi
	kill -TERM "$p" 2>/dev/null
	i=0
	while [ "$i" -lt 30 ]; do
		kill -0 "$p" 2>/dev/null || break
		sleep 0.2 2>/dev/null || sleep 1
		i=$((i + 1))
	done
	if kill -0 "$p" 2>/dev/null; then
		kill -KILL "$p" 2>/dev/null
		sleep 0.2 2>/dev/null || true
	fi
	dpin_write_pid "0"
	dpin_log "guard 已停止 pid=$p"
	return 0
}

dpin_selftest() {
	dpin_load_config
	rc=0
	printf '== dpin-guard selftest ==\n'
	printf 'version=%s\n' "$DPIN_VERSION"
	printf 'state=%s config=%s\n' "$DPIN_STATE" "$DPIN_CONF"
	for f in dpin-lib.sh dpin-guard.sh dpin-recover.sh; do
		if [ -r "$HERE/$f" ]; then
			if sh -n "$HERE/$f" 2>/dev/null; then
				printf 'syntax %-16s OK\n' "$f"
			else
				printf 'syntax %-16s FAIL\n' "$f"
				rc=1
			fi
		else
			printf 'missing %-16s FAIL\n' "$f"
			rc=1
		fi
	done
	on_device=0
	[ -d /system/bin ] && on_device=1
	for c in getprop setprop pidof awk sed grep tr cut sleep date; do
		if command -v "$c" >/dev/null 2>&1; then
			printf 'tool   %-16s OK\n' "$c"
		else
			case $c in
			getprop | setprop | pidof)
				if [ "$on_device" = "1" ]; then
					printf 'tool   %-16s MISSING\n' "$c"
					rc=1
				else
					printf 'tool   %-16s MISSING(host: ok)\n' "$c"
				fi
				;;
			*) printf 'tool   %-16s MISSING\n' "$c" ;;
			esac
		fi
	done
	printf 'root=%s domain=%s\n' "$(id -u 2>/dev/null)" "$(id -Z 2>/dev/null)"
	printf 'config: ENABLE=%s POLL=%s HAL_WAIT=%s VERIFY_DELAY=%s ALLOW_FRAMEWORK_RESTART=%s\n' \
		"$ENABLE" "$POLL" "$HAL_WAIT" "$VERIFY_DELAY" "$ALLOW_FRAMEWORK_RESTART"
	printf 'selftest rc=%s\n' "$rc"
	return "$rc"
}

case ${1:-status} in
help | -h | --help)
	sed -n '2,24p' "$0"
	;;
start) dpin_start ;;
daemon) dpin_daemon ;;
stop) dpin_stop ;;
restart)
	dpin_stop
	dpin_start
	;;
status)
	dpin_load_config
	dpin_classify || true
	dpin_print_status
	exit "$CLASS_CODE"
	;;
verify)
	dpin_load_config
	ss=$(dpin_ss_pid 2>/dev/null || true)
	[ -n "${ss:-}" ] || {
		dpin_log "找不到 system_server"
		exit 1
	}
	dpin_verify_registration "$ss"
	cat "$DPIN_STATE/verify.log" 2>/dev/null | tail -n 1
	;;
boot-report)
	dpin_load_config
	dpin_boot_report
	tail -n 12 "$DPIN_STATE/boot-report.log" 2>/dev/null
	;;
selftest) dpin_selftest ;;
*)
	printf 'usage: dpin-guard.sh {start|daemon|stop|restart|status|verify|boot-report|selftest}\n' >&2
	exit 2
	;;
esac
