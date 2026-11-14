#!/system/bin/sh
# dpin-lib.sh — DP-in 回调守卫生的共同函数（POSIX sh，无 bash 扩展；被 guard/recover 双方 source）
#
# 边界（硬约束，见 FIX-NOTES.md §4）：
#   * 只读系统状态（/proc、getprop、logcat、dumpsys、sysfs 只读）
#   * 唯一写动作：`setprop ctl.start vendor.displayfeatureaidl-hal`（以及 dpin-recover.sh 的
#     显式二次 opt-in 框架重启）
#   * 不写 /odm、不碰 sysfs（只读）、不碰 UDC / typec / i2c、不动触摸模块与 interposer
#
# 可测试性：所有外部依赖都可注入（DPIN_STATE / DPIN_CONF / DPIN_PROC / DPIN_SYSFS / PATH 假命令）。

DPIN_VERSION="0.1.0"

DPIN_STATE=${DPIN_STATE:-/data/adb/mipad_dpin_guard/state}
DPIN_CONF=${DPIN_CONF:-/data/adb/mipad_dpin_guard/config}
DPIN_PROC=${DPIN_PROC:-/proc}
DPIN_SYSFS=${DPIN_SYSFS:-/sys}
DPIN_LOG_TAG=${DPIN_LOG_TAG:-MipadDpinGuard}

# init 服务名 / 进程名 / AIDL 实例（证据来源见 evidence/PIPELINE-TRACE.md §9）
DPIN_HAL_SVC=${DPIN_HAL_SVC:-vendor.displayfeatureaidl-hal}
DPIN_HAL_BIN=${DPIN_HAL_BIN:-vendor.xiaomi.hardware.displayfeature_aidl-service}
DPIN_HAL_IFACE=${DPIN_HAL_IFACE:-IDisplayFeature/default}
DPIN_LOGCAT_TAGS="displaycount DisplayFeatureManagerService DPIN"

# ---- 配置默认值（可被 $DPIN_CONF 覆盖；只接受数字）----
ENABLE=1
POLL=2
HAL_WAIT=12
PHASE500_GUARD=45
MAX_STARTS_PER_FRAMEWORK=3
MAX_STARTS_PER_BOOT=10
START_COOLDOWN=10
VERIFY_DELAY=75
ALLOW_FRAMEWORK_RESTART=0
EARLY_START=0
LOGCAT_TAIL=4000

dpin_load_config() {
	[ -r "$DPIN_CONF" ] || return 0
	while IFS= read -r line; do
		case $line in '' | '#'*) continue ;; esac
		key=${line%%=*}
		val=${line#*=}
		case $key in
		ENABLE | POLL | HAL_WAIT | PHASE500_GUARD | MAX_STARTS_PER_FRAMEWORK | \
		MAX_STARTS_PER_BOOT | START_COOLDOWN | VERIFY_DELAY | ALLOW_FRAMEWORK_RESTART | \
		EARLY_START | LOGCAT_TAIL) : ;;
		*) continue ;;
		esac
		case $val in '' | *[!0-9.]*) continue ;; esac
		case $key in
		ENABLE) ENABLE=$val ;;
		POLL) POLL=$val ;;
		HAL_WAIT) HAL_WAIT=$val ;;
		PHASE500_GUARD) PHASE500_GUARD=$val ;;
		MAX_STARTS_PER_FRAMEWORK) MAX_STARTS_PER_FRAMEWORK=$val ;;
		MAX_STARTS_PER_BOOT) MAX_STARTS_PER_BOOT=$val ;;
		START_COOLDOWN) START_COOLDOWN=$val ;;
		VERIFY_DELAY) VERIFY_DELAY=$val ;;
		ALLOW_FRAMEWORK_RESTART) ALLOW_FRAMEWORK_RESTART=$val ;;
		EARLY_START) EARLY_START=$val ;;
		LOGCAT_TAIL) LOGCAT_TAIL=$val ;;
		esac
	done <"$DPIN_CONF"
	return 0
}

dpin_state_init() {
	mkdir -p "$DPIN_STATE" 2>/dev/null || true
	chmod 700 "$DPIN_STATE" 2>/dev/null || true
	mkdir -p "$DPIN_STATE/events" 2>/dev/null || true
	return 0
}

dpin_ts() { date +%s; }
dpin_hms() { # epoch -> HH:MM:SS（尽力而为；失败原样返回）
	e=$1
	date -d "@$e" '+%H:%M:%S' 2>/dev/null || printf '%s' "$e"
}

# dpin_log <msg...>：stdout + state/events.log + logcat（tag=MipadDpinGuard）
dpin_log() {
	msg=$*
	line="$(date '+%Y-%m-%d %H:%M:%S') $msg"
	printf '%s\n' "$line"
	if [ -d "$DPIN_STATE" ]; then
		printf '%s\n' "$line" >>"$DPIN_STATE/events.log" 2>/dev/null || true
	fi
	if command -v log >/dev/null 2>&1; then
		log -t "$DPIN_LOG_TAG" "$msg" >/dev/null 2>&1 || true
	fi
	return 0
}

dpin_die() { dpin_log "ERROR: $*"; return 1; }

dpin_is_root() { [ "$(id -u 2>/dev/null)" = "0" ]; }

# ---------------------------------------------------------------- 进程/服务探测

dpin_ss_pid() { # system_server pid；优先 pidof，回退 /proc 扫描
	p=$(pidof system_server 2>/dev/null | awk '{print $1; exit}')
	if [ -n "${p:-}" ]; then
		printf '%s\n' "$p"
		return 0
	fi
	for d in "$DPIN_PROC"/[0-9]*; do
		[ -r "$d/comm" ] || continue
		read -r c <"$d/comm" 2>/dev/null || continue
		[ "$c" = "system_server" ] || continue
		printf '%s\n' "${d##*/}"
		return 0
	done
	return 1
}

dpin_hal_pid() { # displayfeature HAL pid（comm 截断到 15 字符，故先按前缀过滤再核 cmdline）
	for d in "$DPIN_PROC"/[0-9]*; do
		[ -r "$d/comm" ] || continue
		read -r c <"$d/comm" 2>/dev/null || continue
		case $c in
		vendor.xiaomi.* | *displayfeature*) : ;;
		*) continue ;;
		esac
		[ -r "$d/cmdline" ] || continue
		cmd=$(tr '\0' ' ' <"$d/cmdline" 2>/dev/null) || continue
		case $cmd in
		*"$DPIN_HAL_BIN"*) printf '%s\n' "${d##*/}"; return 0 ;;
		esac
	done
	return 1
}

dpin_hal_svc_state() { getprop "init.svc.$DPIN_HAL_SVC" 2>/dev/null; }

dpin_hal_published() { # AIDL 服务是否已在 servicemanager 里可见
	if command -v service >/dev/null 2>&1; then
		if service list 2>/dev/null | grep -q "$DPIN_HAL_IFACE"; then return 0; fi
		if service list 2>/dev/null | grep -q "$DPIN_HAL_SVC"; then return 0; fi
		return 1
	fi
	return 2 # 未知
}

dpin_pid_start_epoch() { # /proc/<pid>/stat 第 22 字段 + btime → epoch 秒（取整）
	pid=$1
	stat="$DPIN_PROC/$pid/stat"
	[ -r "$stat" ] || return 1
	bt=$(awk '/^btime /{print $2; exit}' "$DPIN_PROC/stat" 2>/dev/null)
	[ -n "${bt:-}" ] || return 1
	st=$(awk '{n=split($0,a,") "); if(n<2){exit 1} split(a[2],b," "); print b[20]}' "$stat" 2>/dev/null)
	[ -n "${st:-}" ] || return 1
	awk -v b="$bt" -v s="$st" 'BEGIN{printf "%d\n", b + s/100}'
}

dpin_uptime() { awk '{printf "%d\n", $1}' "$DPIN_PROC/uptime" 2>/dev/null; }

# ---------------------------------------------------------------- logcat

dpin_logcat_dump() { # 归一化到 "$DPIN_STATE/.logcat"：单空格分隔 "<epoch> <tag> <msg>"
	out="$DPIN_STATE/.logcat"
	mkdir -p "$DPIN_STATE" 2>/dev/null || true
	if command -v logcat >/dev/null 2>&1; then
		logcat -d -v epoch -b all -t "$LOGCAT_TAIL" -s displaycount DisplayFeatureManagerService DPIN 2>/dev/null |
			awk -v tags="$DPIN_LOGCAT_TAGS" '
			BEGIN{ n=split(tags,t," "); for(i=1;i<=n;i++) want[t[i]]=1 }
			{
				tag=$5; sub(/:$/,"",tag);
				if (!(tag in want)) next;
				msg=$0;
				sub(/^[^ \t]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+[A-Z][ \t]+[^:]+:[ \t]?/,"",msg);
				printf "%s %s %s\n", $1, tag, msg;
			}' >"$out" 2>/dev/null
	else
		: >"$out"
	fi
	return 0
}

dpin_log_last() { # <tag> <msg-ere> → "epoch tag msg"（最后一条）
	awk -v t="$1" -v p="$2" '
		{ if ($2 != t) next; msg=$0; sub(/^[^ ]+ [^ ]+ /,"",msg);
		  if (msg ~ p) l=$0 }
		END { if (l!="") print l }' "$DPIN_STATE/.logcat" 2>/dev/null
}

dpin_log_epoch() { printf '%s' "$1" | cut -d' ' -f1; }
dpin_log_msg() { printf '%s' "$1" | cut -d' ' -f3-; }

dpin_ge() { # a >= b - tol
	awk -v a="$1" -v b="$2" -v t="${3:-0}" 'BEGIN{ exit !((a+0) >= (b+0) - (t+0)) }'
}

dpin_field() { # <text> <key>  → "status=0" 里的 0
	printf '%s' "$1" | sed -n "s/.*[ ,]$2=\([^ ,]*\).*/\1/p"
}

# ---------------------------------------------------------------- 分类

# dpin_classify → 设置 CLASS / CLASS_CODE / REASON，并导出证据变量
#   OK           0  displaycount status=0 → DfMS receive → App params（完整链）
#   OK_NO_CAPTURE 5 完整链但 App 未 startCaptureSession（相机/时序，另案）
#   BROKEN_A     1  displaycount status=0 但 DfMS 无 receive → HAL→Java 注册断（本次黑屏主因）
#   BROKEN_C     2  有 receive 但 App 无 Display params changed → Java→App 断
#   BROKEN_B     3  displaycount status!=0 / DP RX 未起流 → 需物理拔插（软件不可恢复，B1 已证）
#   NO_DPIN      4  最近日志里没有 DP-in 事件（未进入或日志已轮转）
dpin_classify() {
	dpin_logcat_dump
	CLASS=UNKNOWN
	CLASS_CODE=9
	REASON=""
	DC_LINE=$(dpin_log_last displaycount 'Received DP-IN')
	RECV_LINE=$(dpin_log_last DisplayFeatureManagerService 'receive dp in info')
	NOTIFY_LINE=$(dpin_log_last DisplayFeatureManagerService 'notifyDpInCallback')
	APP_LINE=$(dpin_log_last DPIN 'Display params changed')
	CAP_LINE=$(dpin_log_last DPIN 'startCaptureSession')
	HALREG_LINE=$(dpin_log_last displaycount 'callback register')
	DRM_STATUS=$(cat "$DPIN_SYSFS/class/drm/card0-DP-1/status" 2>/dev/null)
	HAS_DP=$(cat "$DPIN_SYSFS/devices/virtual/xm_power/typec/has_dp" 2>/dev/null)

	if [ -z "$DC_LINE" ]; then
		CLASS=NO_DPIN
		CLASS_CODE=4
		REASON="最近 $LOGCAT_TAIL 行日志中没有 'displaycount: Received DP-IN'"
		return 4
	fi

	dc_ep=$(dpin_log_epoch "$DC_LINE")
	dc_msg=$(dpin_log_msg "$DC_LINE")
	dc_status=$(dpin_field "$dc_msg" status)

	if [ "${dc_status:-x}" != "0" ]; then
		CLASS=BROKEN_B
		CLASS_CODE=3
		REASON="displaycount status=${dc_status:-?}（DP RX 未起流）；drm=$(DRM_STATUS:-?) has_dp=$(HAS_DP:-?)"
		return 3
	fi

	if [ -z "$RECV_LINE" ] || ! dpin_ge "$(dpin_log_epoch "$RECV_LINE")" "$dc_ep" 1; then
		CLASS=BROKEN_A
		CLASS_CODE=1
		REASON="displaycount status=0 但 DfMS 无 'receive dp in info' → HAL→Java 回调注册缺失"
		return 1
	fi

	if [ -z "$APP_LINE" ] || ! dpin_ge "$(dpin_log_epoch "$APP_LINE")" "$dc_ep" 1; then
		CLASS=BROKEN_C
		CLASS_CODE=2
		REASON="DfMS 已收到 DP-IN，但 App 无 'Display params changed' → Java→App 断"
		return 2
	fi

	if [ -z "$CAP_LINE" ] || ! dpin_ge "$(dpin_log_epoch "$CAP_LINE")" "$dc_ep" 1; then
		CLASS=OK_NO_CAPTURE
		CLASS_CODE=5
		REASON="回调链已通，但 App 未 startCaptureSession（相机/时序，另案）"
		return 5
	fi

	CLASS=OK
	CLASS_CODE=0
	REASON="displaycount status=0 → DfMS receive → App params → startCaptureSession 全链已通"
	return 0
}

dpin_print_status() {
	printf '== DP-in 链路状态 ==\n'
	printf '时间          : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
	printf 'boot          : btime=%s uptime=%s\n' "$(awk '/^btime /{print $2}' "$DPIN_PROC/stat" 2>/dev/null)" "$(dpin_uptime)"
	ss=$(dpin_ss_pid 2>/dev/null || true)
	hal=$(dpin_hal_pid 2>/dev/null || true)
	printf 'system_server : pid=%s start=%s\n' "${ss:-none}" "$([ -n "${ss:-}" ] && dpin_pid_start_epoch "$ss" 2>/dev/null || echo '?')"
	printf 'displayfeature: pid=%s start=%s init.svc=%s published=%s\n' \
		"${hal:-none}" \
		"$([ -n "${hal:-}" ] && dpin_pid_start_epoch "$hal" 2>/dev/null || echo '?')" \
		"$(dpin_hal_svc_state 2>/dev/null || echo '?')" \
		"$(dpin_hal_published >/dev/null 2>&1 && echo yes || echo no)"
	printf 'DP 链路       : drm(card0-DP-1)=%s has_dp=%s\n' \
		"$(cat "$DPIN_SYSFS/class/drm/card0-DP-1/status" 2>/dev/null)" \
		"$(cat "$DPIN_SYSFS/devices/virtual/xm_power/typec/has_dp" 2>/dev/null)"
	printf '\n-- 最近一次 DP-in 事件的链路证据（epoch / 内容）--\n'
	printf 'displaycount  : %s\n' "${DC_LINE:-<none>}"
	printf 'DfMS receive  : %s\n' "${RECV_LINE:-<none>}"
	printf 'DfMS notify   : %s\n' "${NOTIFY_LINE:-<none>}"
	printf 'App params    : %s\n' "${APP_LINE:-<none>}"
	printf 'startCapture  : %s\n' "${CAP_LINE:-<none>}"
	printf 'HAL register  : %s\n' "${HALREG_LINE:-<none>}"
	printf '\n-- 判定 --\n'
	printf 'CLASS=%s (%s)\n' "$CLASS" "$REASON"
	case $CLASS_CODE in
	0) printf '建议: 完整链已通；若屏幕仍黑，按 FIX-NOTES.md §5 采集 App/相机证据（非本守卫范围）。\n' ;;
	1) printf '建议: 只有框架重启（二次 opt-in）能恢复：dpin-recover.sh recover --yes（见 FIX-NOTES.md §5）\n' ;;
	2) printf '建议: Java→App 断；重进 App 观察，必要时 framework-restart（二次 opt-in）。\n' ;;
	3) printf '建议: DP RX 未起流；软件不可恢复（B1 已证），需物理拔插 USB-C（或整机重启）。\n' ;;
	4) printf '建议: 先进入一次「屏幕扩展pro」，再复查本状态。\n' ;;
	5) printf '建议: 回调链已通但未 startCaptureSession；查 camera 5 / App 时序。\n' ;;
	esac
}

# ---------------------------------------------------------------- HAL 拉起

# dpin_wait_published <timeout_s> → 0 已发布
dpin_wait_published() {
	n=$(awk -v t="$1" 'BEGIN { printf "%d", t * 10 }')
	case $n in '' | *[!0-9]*) n=10 ;; esac
	i=0
	while [ "$i" -lt "$n" ]; do
		if dpin_hal_published >/dev/null 2>&1; then return 0; fi
		sleep 0.1 2>/dev/null || sleep 1
		i=$((i + 1))
	done
	return 1
}

# dpin_ensure_hal <reason> [ss_start_epoch]
# 返回 0=HAL 已可用（原本就在/本次拉起成功）；1=失败；2=被限流
dpin_ensure_hal() {
	reason=${1:-manual}
	ss_start=${2:-0}

	if hal=$(dpin_hal_pid 2>/dev/null); then
		dpin_write_hal_event "present" "pid=$hal" "$reason"
		dpin_log "HAL 已运行（$reason）pid=$hal init.svc=$(dpin_hal_svc_state)"
		return 0
	fi
	if dpin_hal_published >/dev/null 2>&1; then
		# servicemanager 里已可见 ⇒ HAL 活着（进程退出会注销），不做多余重启
		dpin_write_hal_event "published-no-pid" "service=$DPIN_HAL_IFACE" "$reason"
		dpin_log "HAL 服务已发布但 /proc 未扫到进程（$reason）；按「已可用」处理，不重复 ctl.start"
		return 0
	fi
	if [ "${dpin_attempts_fw:-0}" -ge "$MAX_STARTS_PER_FRAMEWORK" ]; then
		dpin_log "HAL 缺失（$reason），但本 framework 世代启动尝试已达上限 $MAX_STARTS_PER_FRAMEWORK，停手"
		dpin_write_hal_event "fw-budget-exhausted" "attempts=$dpin_attempts_fw" "$reason"
		return 2
	fi
	if [ "${dpin_attempts_boot:-0}" -ge "$MAX_STARTS_PER_BOOT" ]; then
		dpin_log "HAL 缺失（$reason），但本 boot 启动尝试已达上限 $MAX_STARTS_PER_BOOT，停手"
		dpin_write_hal_event "budget-exhausted" "attempts=$dpin_attempts_boot" "$reason"
		return 2
	fi
	now=$(dpin_ts)
	if [ "${dpin_last_start:-0}" -gt 0 ] && [ $((now - dpin_last_start)) -lt "$START_COOLDOWN" ]; then
		dpin_log "HAL 缺失（$reason），距上次尝试 $((now - dpin_last_start))s < cooldown ${START_COOLDOWN}s，跳过"
		return 2
	fi

	dpin_attempts_boot=$(( ${dpin_attempts_boot:-0} + 1 ))
	dpin_attempts_fw=$(( ${dpin_attempts_fw:-0} + 1 ))
	dpin_last_start=$now
	dpin_log "HAL 缺失（$reason）→ setprop ctl.start $DPIN_HAL_SVC（本 boot 第 $dpin_attempts_boot 次）"
	setprop ctl.start "$DPIN_HAL_SVC" 2>/dev/null
	rc=$?
	if [ "$rc" != 0 ]; then
		dpin_log "setprop ctl.start 返回 $rc（SELinux/属性写被拒？）"
	fi
	t0=$(dpin_ts)
	if dpin_wait_published "$HAL_WAIT"; then
		hal=$(dpin_hal_pid 2>/dev/null || true)
		lat=$(( $(dpin_ts) - t0 ))
		dpin_log "HAL 已拉起（$reason）pid=${hal:-?} 等待发布 ${lat}s init.svc=$(dpin_hal_svc_state)"
		dpin_write_hal_event "started" "pid=${hal:-?} latency=${lat}s" "$reason"
		if [ "${ss_start:-0}" -gt 0 ]; then
			el=$(( $(dpin_ts) - ss_start ))
			if [ "$el" -gt "$PHASE500_GUARD" ]; then
				dpin_log "WARN: HAL 在 system_server 启动后 ${el}s 才可用（>${PHASE500_GUARD}s），本 boot 注册窗口可能已错过；用 dpin-recover.sh status 判定"
			fi
		fi
		return 0
	fi
	dpin_log "HAL 拉起失败（$reason）：${HAL_WAIT}s 内未在 servicemanager 看到 $DPIN_HAL_IFACE；init.svc=$(dpin_hal_svc_state)"
	dpin_write_hal_event "failed" "wait=${HAL_WAIT}s" "$reason"
	return 1
}

dpin_write_hal_event() { # <result> <detail> <reason>
	mkdir -p "$DPIN_STATE" 2>/dev/null || true
	printf '%s result=%s detail=%s reason=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$3" \
		>>"$DPIN_STATE/hal-events.log" 2>/dev/null || true
}

# ---------------------------------------------------------------- 注册证据

# dpin_verify_registration <ss_pid>
# 在框架启动后检查"HAL 侧是否留下 callback register 证据"；没有则只能算 INCONCLUSIVE
# （HAL 的该行日志格式未在真机验证过，见 FIX-NOTES.md §6 未验证项）。
dpin_verify_registration() {
	ss_pid=${1:-}
	ss_start=0
	[ -n "$ss_pid" ] && ss_start=$(dpin_pid_start_epoch "$ss_pid" 2>/dev/null || echo 0)
	dpin_logcat_dump
	halreg=$(dpin_log_last displaycount 'callback register')
	recv=$(dpin_log_last DisplayFeatureManagerService 'receive dp in info')
	if [ -n "$halreg" ] && dpin_ge "$(dpin_log_epoch "$halreg")" "$ss_start" 1; then
		verdict="REGISTERED(hal-log)"
		detail="$halreg"
	elif [ -n "$recv" ] && dpin_ge "$(dpin_log_epoch "$recv")" "$ss_start" 1; then
		verdict="REGISTERED(dp-in-evidence)"
		detail="$recv"
	else
		verdict="INCONCLUSIVE(no-evidence)"
		detail="HAL 无 'callback register' 行；若尚未进入 DP-in，此结论不代表注册失败"
	fi
	printf '%s ss_pid=%s ss_start=%s verdict=%s detail=%s\n' \
		"$(date '+%Y-%m-%d %H:%M:%S')" "${ss_pid:-?}" "$ss_start" "$verdict" "$detail" \
		>>"$DPIN_STATE/verify.log" 2>/dev/null || true
	dpin_log "注册核查 ss=$ss_pid → $verdict"
	return 0
}

# ---------------------------------------------------------------- boot 报告

dpin_boot_report() {
	mkdir -p "$DPIN_STATE" 2>/dev/null || true
	{
		printf 'time=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
		printf 'boot_id=%s\n' "$(cat "$DPIN_PROC/sys/kernel/random/boot_id" 2>/dev/null)"
		printf 'btime=%s uptime=%s\n' "$(awk '/^btime /{print $2}' "$DPIN_PROC/stat" 2>/dev/null)" "$(dpin_uptime)"
		printf 'ro.boottime.%s=%s\n' "$DPIN_HAL_SVC" "$(getprop "ro.boottime.$DPIN_HAL_SVC" 2>/dev/null)"
		printf 'init.svc.%s=%s\n' "$DPIN_HAL_SVC" "$(dpin_hal_svc_state)"
		printf 'hal_pid=%s hal_start_epoch=%s\n' "$(dpin_hal_pid 2>/dev/null || echo none)" \
			"$([ -n "$(dpin_hal_pid 2>/dev/null || true)" ] && dpin_pid_start_epoch "$(dpin_hal_pid)" 2>/dev/null || echo '?')"
		printf 'ss_pid=%s ss_start_epoch=%s\n' "$(dpin_ss_pid 2>/dev/null || echo none)" \
			"$([ -n "$(dpin_ss_pid 2>/dev/null || true)" ] && dpin_pid_start_epoch "$(dpin_ss_pid)" 2>/dev/null || echo '?')"
		printf 'hal_published=%s\n' "$(dpin_hal_published >/dev/null 2>&1 && echo yes || echo no)"
	} >>"$DPIN_STATE/boot-report.log" 2>/dev/null || true
	return 0
}
