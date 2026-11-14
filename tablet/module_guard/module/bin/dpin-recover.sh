#!/system/bin/sh
# dpin-recover.sh — DP-in 黑屏：检测 / 判定 / 安全的恢复动作（设备端，需 root）
#
# 设计原则（见 FIX-NOTES.md）：
#   * 先说清楚"现在坏在哪"，再动手；判定不出来就明说，不假装成功。
#   * 默认只做无副作用动作（status / check-chain / enter / exit / unlock / snapshot）。
#   * 唯一有副作用的修复是"只重启 system_server"（HAL 保持运行，让新 DfMS 在 phase 500
#     重新注册回调），且必须二次 opt-in：config ALLOW_FRAMEWORK_RESTART=1 + --yes，
#     每 boot 最多一次，失败停手。
#   * 绝不 stop/start 整个 framework（会把 HAL 一起重启，反而重现 bug，B2 已警告）。
#   * DP RX 不起流（BROKEN_B）软件无解（B1 已证），只报"需物理拔插 USB-C"。
#
# 可注入项（仅测试用）：DPIN_KILL（默认 kill；宿主机测试里 sh 的 kill 是内建，需指定外部假命令）、
#   DPIN_EXIT_WAIT、DPIN_RESTART_SETTLE。
#
# 退出码：
#   0 OK / 动作成功
#   1 BROKEN_A（HAL→Java 注册断）：需框架重启（未授权时返回 1）
#   2 BROKEN_C（Java→App 断）
#   3 BROKEN_B（DP RX 未起流，需物理拔插）
#   4 NO_DPIN（未进入 / 日志无事件）
#   5 OK_NO_CAPTURE（链路通但未 startCaptureSession）
#   9 TIMEOUT / 未知
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

# 退出确认弹窗在 OS4.0.16 / 3408x2272 上的坐标（B1 实测，见 evidence/RECOVERY-SEQUENCE.md）
EXIT_DIALOG_MARK=DpinDialog-dpin_exit_monitor
DPIN_KILL=${DPIN_KILL:-kill}
EXIT_DIALOG_TAP_X=${EXIT_DIALOG_TAP_X:-1913}
EXIT_DIALOG_TAP_Y=${EXIT_DIALOG_TAP_Y:-1243}

dpin_require_root() {
	if ! dpin_is_root; then
		dpin_log "需要 root（当前 uid=$(id -u 2>/dev/null)）"
		return 1
	fi
	return 0
}

# ---------------------------------------------------------------- 基本动作

dpin_enter() {
	dpin_log "进入 DP-in：am broadcast -a com.milink.service.notification.enter_monitor -p com.milink.service"
	if ! command -v am >/dev/null 2>&1; then
		dpin_log "ERROR: 没有 am 命令"
		return 1
	fi
	out=$(am broadcast -a com.milink.service.notification.enter_monitor -p com.milink.service 2>&1)
	rc=$?
	printf '%s\n' "$out" | tail -n 3
	if [ "$rc" != 0 ] || ! printf '%s' "$out" | grep -q 'Broadcast completed'; then
		dpin_log "ERROR: 广播失败（rc=$rc）"
		return 1
	fi
	dpin_log "已发送进入广播；等 3s 后可用 check-chain 判定链路"
	sleep 3 2>/dev/null || true
	return 0
}

dpin_exit_dpin() {
	dpin_log "退出 DP-in：keyevent 26 → 等确认弹窗（$EXIT_DIALOG_MARK）→ tap $EXIT_DIALOG_TAP_X $EXIT_DIALOG_TAP_Y"
	input keyevent 26 2>/dev/null
	wait_s=${DPIN_EXIT_WAIT:-10}
	max_iter=$(awk -v t="$wait_s" 'BEGIN { printf "%d", t * 2 }')
	case $max_iter in '' | *[!0-9]*) max_iter=20 ;; esac
	i=0
	found=0
	while [ "$i" -lt "$max_iter" ]; do
		if dumpsys window windows 2>/dev/null | grep -q "$EXIT_DIALOG_MARK"; then
			found=1
			break
		fi
		sleep 0.5 2>/dev/null || sleep 1
		i=$((i + 1))
	done
	if [ "$found" != "1" ]; then
		dpin_log "未看到退出确认弹窗；可能已退出或界面不同，未执行 tap（不做猜测性点击）"
		return 1
	fi
	input tap "$EXIT_DIALOG_TAP_X" "$EXIT_DIALOG_TAP_Y" 2>/dev/null
	sleep 2 2>/dev/null || true
	dpin_log "已点击退出确认"
	return 0
}

dpin_unlock() {
	dpin_log "解锁：keyevent 224（亮屏）+ 82（menu/解锁）"
	input keyevent 224 2>/dev/null
	sleep 0.5 2>/dev/null || true
	input keyevent 82 2>/dev/null
	sleep 1 2>/dev/null || true
	return 0
}

# ---------------------------------------------------------------- 判定

dpin_check_chain() {
	do_enter=0
	do_exit=0
	timeout=25
	while [ $# -gt 0 ]; do
		case $1 in
		--enter) do_enter=1 ;;
		--exit) do_exit=1 ;;
		--timeout)
			shift
			timeout=${1:-25}
			;;
		*) ;;
		esac
		shift
	done
	[ "$do_enter" = 1 ] && dpin_enter
	[ "$do_exit" = 1 ] && dpin_exit_dpin

	t0=$(dpin_ts)
	while :; do
		dpin_classify || true
		case $CLASS_CODE in
		0)
			dpin_print_status
			printf '\nRESULT=PASS class=OK（displaycount status=0 → DfMS receive → App params → startCaptureSession）\n'
			return 0
			;;
		3 | 4) : ;; # 等链路/等进入，继续轮询
		*)
			dpin_print_status
			printf '\nRESULT=FAIL class=%s code=%s\n' "$CLASS" "$CLASS_CODE"
			return "$CLASS_CODE"
			;;
		esac
		if [ $(( $(dpin_ts) - t0 )) -ge "$timeout" ]; then
			break
		fi
		sleep 2 2>/dev/null || sleep 1
	done
	dpin_print_status
	printf '\nRESULT=TIMEOUT class=%s code=%s（%ss 内未出现完整链）\n' "$CLASS" "$CLASS_CODE" "$timeout"
	return 9
}

# ---------------------------------------------------------------- 框架重启（二次 opt-in）

dpin_wait_framework_ready() {
	i=0
	while [ "$i" -lt 120 ]; do
		if command -v service >/dev/null 2>&1; then
			if service check activity 2>/dev/null | grep -q 'found'; then
				return 0
			fi
		else
			return 0
		fi
		sleep 1 2>/dev/null || true
		i=$((i + 1))
	done
	return 1
}

dpin_framework_restart() {
	yes=0
	mech=kill
	dry=0
	while [ $# -gt 0 ]; do
		case $1 in
		--yes) yes=1 ;;
		--mechanism)
			shift
			mech=${1:-kill}
			;;
		--dry-run) dry=1 ;;
		*) ;;
		esac
		shift
	done

	if [ "$ALLOW_FRAMEWORK_RESTART" != "1" ]; then
		dpin_log "拒绝：config ALLOW_FRAMEWORK_RESTART=$ALLOW_FRAMEWORK_RESTART（需显式改为 1，二次 opt-in）"
		return 1
	fi
	if [ "$yes" != "1" ]; then
		dpin_log "拒绝：缺少 --yes（二次 opt-in 的第二次确认）"
		return 1
	fi
	if ! dpin_require_root; then return 1; fi

	hal=$(dpin_hal_pid 2>/dev/null || true)
	if [ -z "$hal" ]; then
		dpin_log "拒绝：displayfeature HAL 不在运行。重启框架无法补注册（新 DfMS 在 phase500 仍会看不到 HAL），先修 HAL"
		return 1
	fi
	ss=$(dpin_ss_pid 2>/dev/null || true)
	if [ -z "$ss" ]; then
		dpin_log "拒绝：找不到 system_server"
		return 1
	fi
	boot_id=$(cat "$DPIN_PROC/sys/kernel/random/boot_id" 2>/dev/null || echo unknown)
	prev_boot=$(awk -F= '/^boot_id=/{print $2; exit}' "$DPIN_STATE/last-framework-restart" 2>/dev/null)
	prev_count=$(awk -F= '/^count=/{print $2; exit}' "$DPIN_STATE/last-framework-restart" 2>/dev/null)
	count=1
	if [ "${prev_boot:-}" = "$boot_id" ]; then
		if [ "${prev_count:-0}" -ge 1 ]; then
			dpin_log "拒绝：本 boot 已尝试过框架重启（count=$prev_count）。失败即停手，改用整机重启/物理拔插"
			return 1
		fi
		count=$(( ${prev_count:-0} + 1 ))
	fi
	ss_start=$(dpin_pid_start_epoch "$ss" 2>/dev/null || echo 0)

	if [ "$dry" = "1" ]; then
		printf 'DRY-RUN 将执行：\n'
		printf '  1) 校验：HAL pid=%s 在运行，system_server pid=%s（start=%s），boot_id=%s\n' "$hal" "$ss" "$ss_start" "$boot_id"
		printf '  2) %s\n' "$([ "$mech" = zygote ] && echo 'setprop ctl.restart zygote' || echo "kill -9 $ss（失败则回退 setprop ctl.restart zygote）")"
		printf '  3) 等新 system_server（≤90s），期间每 10s 确认 HAL 仍在\n'
		printf '  4) 等 framework ready（≤120s）→ 等 20s → verify 注册证据 → status\n'
		printf '  5) 记录 state/last-framework-restart（本 boot count=%s）\n' "$count"
		return 0
	fi

	mkdir -p "$DPIN_STATE" 2>/dev/null || true
	{
		printf 'boot_id=%s\n' "$boot_id"
		printf 'count=%s\n' "$count"
		printf 'ts=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
		printf 'old_ss=%s\n' "$ss"
		printf 'hal=%s\n' "$hal"
		printf 'mechanism=%s\n' "$mech"
		printf 'status=in-progress\n'
	} >"$DPIN_STATE/last-framework-restart" 2>/dev/null || true

	dpin_log "框架重启开始：old_ss=$ss hal=$hal mechanism=$mech（本 boot 第 $count 次，上限 1）"
	mech_used=$mech
	if [ "$mech" = "zygote" ]; then
		setprop ctl.restart zygote 2>/dev/null
		rc=$?
		dpin_log "setprop ctl.restart zygote rc=$rc"
	else
		if "$DPIN_KILL" -9 "$ss" 2>/dev/null; then
			mech_used=kill
			dpin_log "已 kill -9 system_server pid=$ss"
		else
			mech_used=zygote
			dpin_log "kill 被拒（SELinux 信号限制），回退 setprop ctl.restart zygote"
			setprop ctl.restart zygote 2>/dev/null
		fi
	fi
	if [ "$mech_used" != "$mech" ]; then
		dpin_log "机制回退：$mech → $mech_used"
	fi

	# 等新 system_server
	i=0
	new_ss=""
	while [ "$i" -lt 180 ]; do
		new_ss=$(dpin_ss_pid 2>/dev/null || true)
		if [ -n "$new_ss" ] && [ "$new_ss" != "$ss" ]; then break; fi
		if [ $((i % 20)) -eq 0 ] && [ "$i" -gt 0 ]; then
			if [ -z "$(dpin_hal_pid 2>/dev/null || true)" ]; then
				dpin_log "等新框架期间 HAL 缺失 → 补拉"
				dpin_ensure_hal "during-framework-restart" 0 || true
			fi
		fi
		sleep 0.5 2>/dev/null || sleep 1
		i=$((i + 1))
	done
	if [ -z "$new_ss" ] || [ "$new_ss" = "$ss" ]; then
		printf 'status=failed-new-ss-timeout\n' >>"$DPIN_STATE/last-framework-restart" 2>/dev/null || true
		dpin_log "FAIL: 90s 内没等到新的 system_server（old=$ss）；停手，不再重试。请整机重启或物理拔插"
		return 1
	fi
	new_start=$(dpin_pid_start_epoch "$new_ss" 2>/dev/null || echo 0)
	dpin_log "新 system_server pid=$new_ss start=$new_start"

	# 趁 phase500 之前把 HAL 确认好（HAL 本应一直在，这里兜底）
	dpin_ensure_hal "framework-restart-verify" "$new_start" || true

	if ! dpin_wait_framework_ready; then
		printf 'status=failed-framework-ready-timeout\n' >>"$DPIN_STATE/last-framework-restart" 2>/dev/null || true
		dpin_log "FAIL: 120s 内 framework 未 ready；停手"
		return 1
	fi
	settle=${DPIN_RESTART_SETTLE:-20}
	dpin_log "framework 已 ready，等待 ${settle}s 稳定后核查注册"
	[ "$settle" = "0" ] || sleep "$settle" 2>/dev/null || true
	dpin_verify_registration "$new_ss" || true
	printf 'status=done new_ss=%s new_start=%s ts=%s\n' "$new_ss" "$new_start" "$(date '+%Y-%m-%d %H:%M:%S')" \
		>>"$DPIN_STATE/last-framework-restart" 2>/dev/null || true
	dpin_classify || true
	dpin_print_status
	printf '\nRESULT=RESTARTED pid=%s（下一步：若 DP 链路断（BROKEN_B）先物理拔插 USB-C，再 dpin-recover.sh enter 并 check-chain）\n' "$new_ss"
	return 0
}

# ---------------------------------------------------------------- snapshot

dpin_snapshot() {
	dir=""
	while [ $# -gt 0 ]; do
		case $1 in
		--dir)
			shift
			dir=${1:-}
			;;
		*) ;;
		esac
		shift
	done
	[ -n "$dir" ] || dir="/data/local/tmp/dpin-snapshot-$(date +%Y%m%d-%H%M%S)"
	mkdir -p "$dir" 2>/dev/null || {
		dpin_log "ERROR: 无法创建 $dir"
		return 1
	}
	dpin_load_config
	dpin_classify || true
	dpin_print_status >"$dir/status.txt" 2>&1
	dpin_logcat_dump
	cp "$DPIN_STATE/.logcat" "$dir/logcat-tags.txt" 2>/dev/null || true
	if command -v logcat >/dev/null 2>&1; then
		logcat -d -v epoch -b all -t 2000 >"$dir/logcat-tail.txt" 2>/dev/null || true
	fi
	getprop >"$dir/props.txt" 2>/dev/null || true
	if command -v ps >/dev/null 2>&1; then
		ps -A 2>/dev/null | grep -E 'system_server|displayfeature' >"$dir/ps.txt" 2>/dev/null || true
	fi
	{
		printf 'drm(card0-DP-1)=%s\n' "$(cat "$DPIN_SYSFS/class/drm/card0-DP-1/status" 2>/dev/null)"
		printf 'has_dp=%s\n' "$(cat "$DPIN_SYSFS/devices/virtual/xm_power/typec/has_dp" 2>/dev/null)"
		printf 'init.svc.%s=%s\n' "$DPIN_HAL_SVC" "$(dpin_hal_svc_state)"
	} >"$dir/dp-link.txt" 2>/dev/null || true
	if command -v dumpsys >/dev/null 2>&1; then
		dumpsys window windows 2>/dev/null | grep -E 'DpinDisplayOverlay|DisplayModeActivity|DpinDialog' >"$dir/window-dpin.txt" 2>/dev/null || true
	fi
	if command -v screencap >/dev/null 2>&1; then
		screencap -p >"$dir/screen.png" 2>/dev/null || true
	fi
	dpin_log "snapshot 已写入 $dir"
	printf 'SNAPSHOT=%s\n' "$dir"
	return 0
}

# ---------------------------------------------------------------- 配置开关

dpin_allow_restart() {
	val=${1:-}
	case $val in
	on | 1) val=1 ;;
	off | 0) val=0 ;;
	*)
		printf 'usage: dpin-recover.sh allow-restart on|off\n' >&2
		return 2
		;;
	esac
	tmp="$DPIN_CONF.tmp.$$"
	{
		if [ -r "$DPIN_CONF" ]; then
			grep -v '^ALLOW_FRAMEWORK_RESTART=' "$DPIN_CONF"
		fi
		printf 'ALLOW_FRAMEWORK_RESTART=%s\n' "$val"
	} >"$tmp" 2>/dev/null || {
		dpin_log "ERROR: 无法写 $tmp"
		return 1
	}
	mv "$tmp" "$DPIN_CONF" 2>/dev/null || {
		dpin_log "ERROR: 无法更新 $DPIN_CONF"
		return 1
	}
	dpin_log "config: ALLOW_FRAMEWORK_RESTART=$val（$DPIN_CONF）"
	return 0
}

# ---------------------------------------------------------------- recover 主流程

dpin_recover() {
	yes=0
	while [ $# -gt 0 ]; do
		case $1 in
		--yes) yes=1 ;;
		*) ;;
		esac
		shift
	done
	dpin_classify || true
	dpin_print_status
	case $CLASS_CODE in
	0)
		printf '\nRESULT=OK 无需恢复\n'
		return 0
		;;
	3)
		printf '\nRESULT=BLOCKED-LINK（displaycount status!=0，DP RX 未起流）\n'
		printf '软件不可恢复（B1 已证：UDC/typec/HAL/gadget/i2c/dpms 全部无效）。请物理拔插 USB-C，或整机重启。\n'
		return 3
		;;
	4)
		printf '\nRESULT=NO-DPIN 最近日志没有 DP-in 事件\n'
		printf '请先用正式入口进入一次「屏幕扩展pro」：dpin-recover.sh enter；然后 dpin-recover.sh check-chain\n'
		return 4
		;;
	1)
		printf '\nRESULT=NEED-FRAMEWORK-RESTART（BROKEN_A：HAL→Java 注册断）\n'
		if [ "$ALLOW_FRAMEWORK_RESTART" = "1" ] && [ "$yes" = "1" ]; then
			dpin_framework_restart --yes
			return $?
		fi
		printf '恢复手段只有"只重启 system_server"（HAL 保持运行），这是二次 opt-in 动作：\n'
		printf '  1) dpin-recover.sh allow-restart on   # 写 config ALLOW_FRAMEWORK_RESTART=1\n'
		printf '  2) dpin-recover.sh framework-restart --dry-run\n'
		printf '  3) dpin-recover.sh framework-restart --yes\n'
		printf '（每 boot 最多一次；失败停手，改用整机重启）\n'
		return 1
		;;
	2 | 5)
		printf '\nRESULT=%s（code=%s）\n' "$CLASS" "$CLASS_CODE"
		printf '建议：用正式入口退出（dpin-recover.sh exit）后重新进入（enter），再 check-chain；\n'
		printf '若仍为 BROKEN_C/OK_NO_CAPTURE，采集 snapshot 交回分析（本脚本不自动 force-stop milink）。\n'
		return "$CLASS_CODE"
		;;
	*)
		printf '\nRESULT=UNKNOWN\n'
		return 9
		;;
	esac
}

usage() {
	cat <<'EOF'
dpin-recover.sh — DP-in 黑屏检测/恢复（设备端，需 root）

用法:
  dpin-recover.sh status                     判定当前链路状态（只读）
  dpin-recover.sh check-chain [--enter] [--timeout N]
                                             等完整回调链；PASS/FAIL/TIMEOUT
  dpin-recover.sh enter                      进入 DP-in（OSD 的等效广播入口）
  dpin-recover.sh exit                       退出 DP-in（power 键 + 确认弹窗）
  dpin-recover.sh unlock                     亮屏并解锁（无 PIN 时）
  dpin-recover.sh recover [--yes]            按判定结果给出/执行恢复
  dpin-recover.sh framework-restart [--dry-run] [--yes] [--mechanism kill|zygote]
                                             只重启 system_server（HAL 保持运行）
  dpin-recover.sh snapshot [--dir DIR]       采集证据（日志/状态/截图）
  dpin-recover.sh allow-restart on|off       写 config ALLOW_FRAMEWORK_RESTART
  dpin-recover.sh selftest                   自检
  dpin-recover.sh version                    版本

退出码: 0 OK / 1 BROKEN_A / 2 BROKEN_C / 3 BROKEN_B / 4 NO_DPIN / 5 OK_NO_CAPTURE / 9 TIMEOUT
EOF
}

cmd=${1:-status}
[ $# -gt 0 ] && shift
dpin_load_config
case $cmd in
status)
	dpin_classify || true
	dpin_print_status
	exit "$CLASS_CODE"
	;;
check-chain) dpin_check_chain "$@" ;;
enter) dpin_require_root && dpin_enter ;;
exit) dpin_require_root && dpin_exit_dpin ;;
unlock) dpin_require_root && dpin_unlock ;;
recover) dpin_recover "$@" ;;
framework-restart) dpin_framework_restart "$@" ;;
snapshot) dpin_snapshot "$@" ;;
allow-restart) dpin_allow_restart "$@" ;;
selftest)
	rc=0
	printf 'version=%s\n' "$DPIN_VERSION"
	for f in dpin-lib.sh dpin-recover.sh; do
		if sh -n "$HERE/$f" 2>/dev/null; then printf 'syntax %-16s OK\n' "$f"; else printf 'syntax %-16s FAIL\n' "$f"; rc=1; fi
	done
	printf 'root=%s domain=%s\n' "$(id -u 2>/dev/null)" "$(id -Z 2>/dev/null)"
	printf 'config: ALLOW_FRAMEWORK_RESTART=%s HAL_WAIT=%s\n' "$ALLOW_FRAMEWORK_RESTART" "$HAL_WAIT"
	exit "$rc"
	;;
version) printf '%s\n' "$DPIN_VERSION" ;;
help | -h | --help) usage ;;
*)
	usage >&2
	exit 2
	;;
esac
