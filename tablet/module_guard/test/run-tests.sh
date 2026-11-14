#!/bin/sh
# run-tests.sh — DP-in 回调守卫 / 恢复 helper 的离线宿主机测试（不接设备、不改系统）
#
# 做法：每个用例一个独立沙箱（$ROOT/<case>/），把外部依赖全部替换成假命令（PATH 优先）：
#   getprop setprop pidof service logcat log kill id am input dumpsys screencap ps sleep
# 并把 /proc、/sys 用 DPIN_PROC / DPIN_SYSFS 指到沙箱里的假文件。
# 脚本一律用 dash 跑（比 Android mksh 更严格），以暴露 bash 扩展。
#
# 用法: ./run-tests.sh [用例名过滤]
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
FIX=$(cd "$HERE/.." && pwd)
MODDIR="$FIX/module"
GUARD="$MODDIR/bin/dpin-guard.sh"
RECOVER="$MODDIR/bin/dpin-recover.sh"
HOSTWRAP="$FIX/host/dpin-recover-host.sh"
SHBIN=$(command -v dash 2>/dev/null || command -v sh)
PATHBASE=$PATH
FILTER=${1:-}
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dpin-tests.XXXXXX") || exit 1
BTIME=1794502896
PASS=0
FAIL=0
FAILED=""

printf '== DP-in guard 离线测试 ==\n'
printf 'sandbox : %s\nshell   : %s\n\n' "$ROOT" "$SHBIN"

# ---------------------------------------------------------------- 断言

pass() {
	PASS=$((PASS + 1))
	printf 'PASS  %s\n' "$1"
}
fail() {
	FAIL=$((FAIL + 1))
	FAILED="$FAILED
  - $1"
	printf 'FAIL  %s\n' "$1"
}
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1（期望 [$3] 实得 [$2]）"; fi; }
assert_contains() { if printf '%s\n' "$2" | grep -q -F -- "$3"; then pass "$1"; else fail "$1（输出里没有 [$3]）"; fi; }
assert_not_contains() { if printf '%s\n' "$2" | grep -q -F -- "$3"; then fail "$1（输出里意外有 [$3]）"; else pass "$1"; fi; }
assert_file_has() { if [ -f "$2" ] && grep -q -F -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1（$2 里没有 [$3]）"; fi; }
assert_file_absent() { if [ ! -f "$2" ] || ! grep -q -F -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1（$2 意外含有 [$3]）"; fi; }
assert_file_missing() { if [ -e "$2" ]; then fail "$1（$2 存在）"; else pass "$1"; fi; }
assert_dir() { if [ -d "$2" ]; then pass "$1"; else fail "$1（缺目录 $2）"; fi; }

# ---------------------------------------------------------------- 沙箱

mk_shims() {
	d=$1
	mkdir -p "$d/bin"

	cat >"$d/bin/id" <<'EOF'
#!/bin/sh
case ${1:-} in
-u) echo 0 ;;
-Z) echo 'u:r:ksu:s0' ;;
*) echo 'uid=0(root) gid=0(root)' ;;
esac
EOF

	cat >"$d/bin/log" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$SB/out"
printf '%s\n' "$*" >>"$SB/out/log.log"
EOF

	cat >"$d/bin/getprop" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
f="$SB/fx/props"
[ -f "$f" ] || exit 0
if [ $# -eq 0 ]; then cat "$f"; exit 0; fi
awk -F= -v k="$1" '$1==k { print substr($0,index($0,"=")+1); found=1 } END { if (!found) print "" }' "$f"
EOF

	cat >"$d/bin/setprop" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$SB/out"
printf '%s=%s\n' "${1:-}" "${2:-}" >>"$SB/out/setprop.log"
if [ "${1:-}" = "ctl.start" ]; then
	n=$(cat "$SB/fx/hal_starts_left" 2>/dev/null || echo 0)
	case $n in '' | *[!0-9]*) n=0 ;; esac
	if [ "$n" -gt 0 ]; then
		n=$((n - 1))
		printf '%s\n' "$n" >"$SB/fx/hal_starts_left"
		hp=$(cat "$SB/fx/hal_pid_to_start" 2>/dev/null || echo 4242)
		st=$(cat "$SB/fx/hal_starttime" 2>/dev/null || echo 520)
		sh "$SB/mkproc.sh" "$SB/proc" "$hp" vendor.xiaomi.h /vendor/bin/hw/vendor.xiaomi.hardware.displayfeature_aidl-service "$st"
		printf '0 vendor.xiaomi.hardware.displayfeature_aidl.IDisplayFeature/default: []\n' >>"$SB/fx/services.txt"
	fi
fi
exit 0
EOF

	cat >"$d/bin/pidof" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
case ${1:-} in
system_server) cat "$SB/fx/pidof_system_server" 2>/dev/null || true ;;
*) : ;;
esac
EOF

	cat >"$d/bin/service" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
case ${1:-} in
list) cat "$SB/fx/services.txt" 2>/dev/null || true ;;
check) printf 'Service %s: found\n' "${2:-}" ;;
*) : ;;
esac
EOF

	cat >"$d/bin/kill" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$SB/out"
printf '%s\n' "$*" >>"$SB/out/kill.log"
sig=${1:-}
pid=${2:-}
case $sig in
-0)
	grep -qx "$pid" "$SB/fx/alive_pids" 2>/dev/null && exit 0
	exit 1
	;;
-9)
	if [ -f "$SB/fx/new_ss_pid" ]; then
		np=$(cat "$SB/fx/new_ss_pid")
		printf '%s\n' "$np" >"$SB/fx/pidof_system_server"
		printf '%s\n' "$np" >>"$SB/fx/alive_pids"
		nst=$(cat "$SB/fx/new_ss_starttime" 2>/dev/null || echo 700)
		sh "$SB/mkproc.sh" "$SB/proc" "$np" system_server /system/bin/system_server "$nst"
	fi
	;;
-TERM | -KILL)
	if [ -f "$SB/fx/alive_pids" ]; then
		grep -vx "$pid" "$SB/fx/alive_pids" >"$SB/out/alive.tmp" 2>/dev/null || true
		mv "$SB/out/alive.tmp" "$SB/fx/alive_pids"
	fi
	;;
esac
exit 0
EOF

	cat >"$d/bin/logcat" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
cat "$SB/fx/logcat.txt" 2>/dev/null || true
EOF

	cat >"$d/bin/am" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$SB/out"
printf '%s\n' "$*" >>"$SB/out/am.log"
echo "Broadcasting: Intent { act=... }"
echo "Broadcast completed: result=0"
EOF

	cat >"$d/bin/input" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$SB/out"
printf '%s\n' "$*" >>"$SB/out/input.log"
EOF

	cat >"$d/bin/dumpsys" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
case ${1:-} in
window) cat "$SB/fx/window.txt" 2>/dev/null || true ;;
*) echo "" ;;
esac
EOF

	cat >"$d/bin/screencap" <<'EOF'
#!/bin/sh
printf 'PNG-FAKE\n'
EOF

	cat >"$d/bin/ps" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
cat "$SB/fx/ps.txt" 2>/dev/null || true
EOF

	cat >"$d/bin/sleep" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
n=$(cat "$SB/fx/sleep_count" 2>/dev/null || echo 0)
case $n in '' | *[!0-9]*) n=0 ;; esac
n=$((n + 1))
printf '%s\n' "$n" >"$SB/fx/sleep_count"
at=$(cat "$SB/fx/sleep_action_at" 2>/dev/null || echo 0)
if [ "$at" != "0" ] && [ "$n" -eq "$at" ] && [ -f "$SB/fx/sleep_action" ]; then
	sh "$SB/fx/sleep_action"
fi
exec /bin/sleep "${1:-1}"
EOF

	chmod 755 "$d/bin/"*
}

mk_mkproc() {
	d=$1
	cat >"$d/mkproc.sh" <<'EOF'
#!/bin/sh
# mkproc.sh <procroot> <pid> <comm> <cmdline> <starttime>
p=$1
pid=$2
comm=$3
cmd=$4
st=$5
d="$p/$pid"
mkdir -p "$d"
printf '%s\n' "$comm" >"$d/comm"
printf '%s' "$cmd" >"$d/cmdline"
i=1
rest=""
while [ "$i" -le 50 ]; do
	if [ "$i" -eq 20 ]; then rest="$rest $st"; else rest="$rest 0"; fi
	i=$((i + 1))
done
printf '%s (%s) S%s\n' "$pid" "$comm" "$rest" >"$d/stat"
EOF
	chmod 755 "$d/mkproc.sh"
}

write_default_config() {
	cat >"$1/config" <<'EOF'
ENABLE=1
POLL=0.05
HAL_WAIT=2
PHASE500_GUARD=999999
MAX_STARTS_PER_FRAMEWORK=2
MAX_STARTS_PER_BOOT=3
START_COOLDOWN=0
VERIFY_DELAY=0
ALLOW_FRAMEWORK_RESTART=0
EARLY_START=0
LOGCAT_TAIL=4000
EOF
}

new_sb() {
	d="$ROOT/$1"
	mkdir -p "$d/bin" "$d/state" "$d/out" "$d/fx"
	mkdir -p "$d/proc/sys/kernel/random"
	mkdir -p "$d/sys/class/drm/card0-DP-1" "$d/sys/devices/virtual/xm_power/typec"
	printf 'btime %s\ncpu  0 0 0 0\n' "$BTIME" >"$d/proc/stat"
	printf '12345.67 1234.00\n' >"$d/proc/uptime"
	printf 'test-boot-0001\n' >"$d/proc/sys/kernel/random/boot_id"
	printf 'connected\n' >"$d/sys/class/drm/card0-DP-1/status"
	printf '1\n' >"$d/sys/devices/virtual/xm_power/typec/has_dp"
	printf 'init.svc.vendor.displayfeatureaidl-hal=stopped\nro.boottime.vendor.displayfeatureaidl-hal=9005209472\nro.product.device=yingtian\nro.product.model=M367FC\n' >"$d/fx/props"
	touch "$d/fx/services.txt" "$d/fx/logcat.txt" "$d/fx/window.txt" "$d/fx/ps.txt" "$d/fx/alive_pids"
	printf '0\n' >"$d/fx/hal_starts_left"
	printf '4242\n' >"$d/fx/hal_pid_to_start"
	printf '520\n' >"$d/fx/hal_starttime"
	printf '0\n' >"$d/fx/sleep_count"
	write_default_config "$d"
	mk_mkproc "$d"
	mk_shims "$d"
	printf '%s' "$d"
}

setup_env() {
	export DPIN_STATE="$1/state"
	export DPIN_CONF="$1/config"
	export DPIN_PROC="$1/proc"
	export DPIN_SYSFS="$1/sys"
	export DPIN_RESTART_SETTLE=0
	export PATH="$1/bin:$PATHBASE"
}

# 组装一个"已存在且健康"的 pid（在沙箱 proc 里）
mkproc() { sh "$1/mkproc.sh" "$1/proc" "$2" "$3" "$4" "$5"; }

# logcat 夹具：正常链（displaycount → DfMS receive → DfMS notify → App params → startCapture）
fixture_ok_chain() {
	cat >"$1/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=3408, height=2272, framerate=120, data_format=0, color_depth=8, status=0
1763071966.055  24032 24032 I DisplayFeatureManagerService: DP-IN callback registered: true
1763071966.060  24032 24032 I DisplayFeatureManagerService: receive dp in info: width=3408, height=2272, framerate=120, status=0
1763071966.061  24032 24032 I DisplayFeatureManagerService: notifyDpInCallback: width=3408, height=2272, framerate=120, status=0
1763071966.070  24032 24032 I DPIN    : AppLifecycleManager Display params changed: width=3408, height=2272, frameRate=120, dataFormat=0, colorDepth=8, status=0
1763071966.090  24032 24032 I DPIN    : startCaptureSession surfaceSize 3408x2272
EOF
}

# logcat 夹具：坏链（HAL→Java 注册断；App 自己的注册行仍在，不能骗过分类器）
fixture_broken_a() {
	cat >"$1/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I DisplayFeatureManagerService: DP-IN callback registered: true
1763071966.070  24032 24032 I DPIN    : DisplayModeActivity Skipping surfaceChanged: displayParam not ready
1763071966.080  24032 24032 I displaycount: Received DP-IN: width=3408, height=2272, framerate=120, data_format=0, color_depth=8, status=0
EOF
}

# 让 system_server / HAL 都活着（可选：准备"框架重启后新 pid"）
fixture_live_procs() {
	sb=$1
	sspid=${2:-3000}
	halpid=${3:-4000}
	printf '%s\n' "$sspid" >"$sb/fx/pidof_system_server"
	: >>"$sb/fx/alive_pids"
	printf '%s\n' "$sspid" >>"$sb/fx/alive_pids"
	printf '%s\n' "$halpid" >>"$sb/fx/alive_pids"
	mkproc "$sb" "$sspid" system_server /system/bin/system_server 500
	mkproc "$sb" "$halpid" vendor.xiaomi.h /vendor/bin/hw/vendor.xiaomi.hardware.displayfeature_aidl-service 400
	printf '0 vendor.xiaomi.hardware.displayfeature_aidl.IDisplayFeature/default: []\n' >"$sb/fx/services.txt"
}

want() {
	[ -z "$FILTER" ] && return 0
	case $1 in *"$FILTER"*) return 0 ;; esac
	return 1
}

run_case() {
	name=$1
	if want "$name"; then
		"$name"
	fi
}

# ================================================================ 用例

# T01：candidate 启动 26ms 后 HAL 缺失 → 守护在 phase500 前 setprop ctl.start，且只拉一次
t01_guard_pulls_hal_at_framework_start() {
	sb=$(new_sb t01)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4010 >/dev/null
	# 把"已有 HAL"去掉，制造本 boot 的竞态（HAL 在 framework 启动后 23s 才起）
	mv "$sb/proc/4010" "$sb/out/proc-4010-removed"
	printf '' >"$sb/fx/services.txt"
	printf '1\n' >"$sb/fx/hal_starts_left"
	DPIN_DAEMON_MAX_LOOPS=4 "$SHBIN" "$GUARD" daemon >"$sb/out/daemon.out" 2>&1
	assert_file_has "T01 守护调用 ctl.start" "$sb/out/setprop.log" "ctl.start=vendor.displayfeatureaidl-hal"
	assert_eq "T01 只启动一次" "$(grep -c 'ctl.start=' "$sb/out/setprop.log" 2>/dev/null || echo 0)" "1"
	assert_file_has "T01 记录 HAL 已拉起" "$sb/state/events.log" "HAL 已拉起"
	assert_dir "T01 HAL 进程出现在 proc" "$sb/proc/4242"
	assert_file_has "T01 注册核查留痕" "$sb/state/verify.log" "INCONCLUSIVE"
}

# T02：HAL 本来就在 → 绝不 setprop（零风险路径）
t02_guard_noop_when_hal_present() {
	sb=$(new_sb t02)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	DPIN_DAEMON_MAX_LOOPS=3 "$SHBIN" "$GUARD" daemon >"$sb/out/daemon.out" 2>&1
	assert_file_missing "T02 未调用 setprop" "$sb/out/setprop.log"
	assert_file_has "T02 记录 HAL 已运行" "$sb/state/events.log" "HAL 已运行"
	assert_file_absent "T02 无拉起失败" "$sb/state/events.log" "拉起失败"
}

# T03：HAL 拉不起来 → 有界重试后停手（不无限刷）
t03_guard_start_failure_is_bounded() {
	sb=$(new_sb t03)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4010 >/dev/null
	mv "$sb/proc/4010" "$sb/out/proc-4010-removed"
	printf '' >"$sb/fx/services.txt"
	printf '0\n' >"$sb/fx/hal_starts_left"
	sed -i.bak 's/^MAX_STARTS_PER_FRAMEWORK=.*/MAX_STARTS_PER_FRAMEWORK=1/' "$sb/config"
	sed -i.bak 's/^HAL_WAIT=.*/HAL_WAIT=0.3/' "$sb/config"
	DPIN_DAEMON_MAX_LOOPS=6 "$SHBIN" "$GUARD" daemon >"$sb/out/daemon.out" 2>&1
	assert_eq "T03 只尝试 1 次" "$(grep -c 'ctl.start=' "$sb/out/setprop.log" 2>/dev/null || echo 0)" "1"
	assert_file_has "T03 记录拉起失败" "$sb/state/events.log" "拉起失败"
	assert_file_has "T03 达上限后停手" "$sb/state/events.log" "启动尝试已达上限"
}

# T04：framework 启动后 HAL 重启过 → 明确警告"注册已丢失"（不假装能修）
t04_guard_warns_on_hal_restart_after_framework() {
	sb=$(new_sb t04)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/sleep_action" <<EOF
#!/bin/sh
mv "$sb/proc/4000" "$sb/out/proc-4000-old"
sh "$sb/mkproc.sh" "$sb/proc" 4100 vendor.xiaomi.h /vendor/bin/hw/vendor.xiaomi.hardware.displayfeature_aidl-service 600
EOF
	chmod 755 "$sb/fx/sleep_action"
	printf '3\n' >"$sb/fx/sleep_action_at"
	DPIN_DAEMON_MAX_LOOPS=5 "$SHBIN" "$GUARD" daemon >"$sb/out/daemon.out" 2>&1
	assert_file_has "T04 警告 HAL 在框架后重启" "$sb/state/events.log" "HAL 在 framework 启动后重启"
	assert_file_has "T04 指向 recover" "$sb/state/events.log" "dpin-recover.sh recover --yes"
}

# T05：坏链判定 = BROKEN_A（App 自己的 "DP-IN callback registered: true" 不能骗过分类器）
t05_status_detects_broken_a() {
	sb=$(new_sb t05)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_broken_a "$sb"
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	rc=$?
	assert_eq "T05 退出码 1" "$rc" "1"
	assert_file_has "T05 判定 BROKEN_A" "$sb/out/status.txt" "CLASS=BROKEN_A"
	assert_file_has "T05 归因 HAL→Java" "$sb/out/status.txt" "HAL→Java"
	assert_file_has "T05 提示这是二次 opt-in" "$sb/out/status.txt" "二次 opt-in"
}

# T06：完整链判定 = OK
t06_status_detects_ok() {
	sb=$(new_sb t06)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_ok_chain "$sb"
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	rc=$?
	assert_eq "T06 退出码 0" "$rc" "0"
	assert_file_has "T06 判定 CLASS=OK" "$sb/out/status.txt" "CLASS=OK"
}

# T07：displaycount status!=0 → BROKEN_B（软件无解，必须物理拔插）
t07_status_detects_broken_b() {
	sb=$(new_sb t07)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=0, height=0, framerate=0, data_format=0, color_depth=0, status=1
EOF
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	rc=$?
	assert_eq "T07 退出码 3" "$rc" "3"
	assert_file_has "T07 判定 BROKEN_B" "$sb/out/status.txt" "CLASS=BROKEN_B"
}

# T08：没有 DP-in 事件 → NO_DPIN
t08_status_no_dpin() {
	sb=$(new_sb t08)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	rc=$?
	assert_eq "T08 退出码 4" "$rc" "4"
	assert_file_has "T08 判定 NO_DPIN" "$sb/out/status.txt" "CLASS=NO_DPIN"
}

# T09：上一轮的陈旧 DfMS receive 不能被当成本轮证据（epoch 必须 >= displaycount）
t09_stale_receive_does_not_count() {
	sb=$(new_sb t09)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/logcat.txt" <<'EOF'
1763071000.000  24032 24032 I DisplayFeatureManagerService: receive dp in info: width=3408, height=2272, framerate=120, status=0
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=3408, height=2272, framerate=120, data_format=0, color_depth=8, status=0
EOF
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	rc=$?
	assert_eq "T09 退出码 1（陈旧证据不算）" "$rc" "1"
	assert_file_has "T09 判定 BROKEN_A" "$sb/out/status.txt" "CLASS=BROKEN_A"
}

# T10：recover 默认拒绝带副作用的修复（二次 opt-in）
t10_recover_refuses_without_optin() {
	sb=$(new_sb t10)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_broken_a "$sb"
	"$SHBIN" "$RECOVER" recover --yes >"$sb/out/rec.txt" 2>&1
	rc=$?
	assert_eq "T10 退出码 1" "$rc" "1"
	assert_file_has "T10 提示二次 opt-in" "$sb/out/rec.txt" "二次 opt-in"
	assert_file_missing "T10 未执行 kill" "$sb/out/kill.log"
}

# T11：allow=1 但没有 --yes → 仍然拒绝
t11_framework_restart_requires_yes() {
	sb=$(new_sb t11)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	printf 'ALLOW_FRAMEWORK_RESTART=1\n' >>"$sb/config"
	"$SHBIN" "$RECOVER" framework-restart >"$sb/out/fr.txt" 2>&1
	rc=$?
	assert_eq "T11 退出码 1" "$rc" "1"
	assert_file_has "T11 要求 --yes" "$sb/out/fr.txt" "--yes"
	assert_file_missing "T11 未执行 kill" "$sb/out/kill.log"
}

# T12：二次 opt-in 齐备 → 只重启 system_server，HAL 不动，并留档
t12_framework_restart_happy_path() {
	sb=$(new_sb t12)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	printf 'ALLOW_FRAMEWORK_RESTART=1\n' >>"$sb/config"
	printf '3001\n' >"$sb/fx/new_ss_pid"
	printf '700\n' >"$sb/fx/new_ss_starttime"
	DPIN_KILL="$sb/bin/kill" "$SHBIN" "$RECOVER" framework-restart --yes >"$sb/out/fr.txt" 2>&1
	rc=$?
	assert_eq "T12 退出码 0" "$rc" "0"
	assert_file_has "T12 kill -9 旧 system_server" "$sb/out/kill.log" "-9 3000"
	assert_file_has "T12 结果" "$sb/out/fr.txt" "RESULT=RESTARTED pid=3001"
	assert_file_has "T12 留档 count=1" "$sb/state/last-framework-restart" "count=1"
	assert_file_has "T12 留档 done" "$sb/state/last-framework-restart" "status=done"
	# HAL 进程必须还在（不能被一起重启）
	assert_dir "T12 HAL 仍活着" "$sb/proc/4000"
}

# T13：同一个 boot 第二次重启被拒（失败即停手）
t13_framework_restart_once_per_boot() {
	sb=$(new_sb t13)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	printf 'ALLOW_FRAMEWORK_RESTART=1\n' >>"$sb/config"
	printf '3001\n' >"$sb/fx/new_ss_pid"
	printf '700\n' >"$sb/fx/new_ss_starttime"
	DPIN_KILL="$sb/bin/kill" "$SHBIN" "$RECOVER" framework-restart --yes >"$sb/out/fr1.txt" 2>&1
	printf '3002\n' >"$sb/fx/new_ss_pid"
	DPIN_KILL="$sb/bin/kill" "$SHBIN" "$RECOVER" framework-restart --yes >"$sb/out/fr2.txt" 2>&1
	rc=$?
	assert_eq "T13 第二次退出码 1" "$rc" "1"
	assert_file_has "T13 拒绝理由" "$sb/out/fr2.txt" "本 boot 已尝试过"
}

# T14：check-chain --enter 在健康链上 PASS
t14_check_chain_pass() {
	sb=$(new_sb t14)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_ok_chain "$sb"
	"$SHBIN" "$RECOVER" check-chain --enter >"$sb/out/chk.txt" 2>&1
	rc=$?
	assert_eq "T14 退出码 0" "$rc" "0"
	assert_file_has "T14 RESULT=PASS" "$sb/out/chk.txt" "RESULT=PASS"
	assert_file_has "T14 走了正式广播入口" "$sb/out/am.log" "com.milink.service.notification.enter_monitor"
}

# T15：check-chain 在坏链上 FAIL（不假装成功）
t15_check_chain_fail() {
	sb=$(new_sb t15)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_broken_a "$sb"
	sed -i.bak 's/^VERIFY_DELAY=.*/VERIFY_DELAY=0/' "$sb/config"
	"$SHBIN" "$RECOVER" check-chain --timeout 1 >"$sb/out/chk.txt" 2>&1
	rc=$?
	assert_eq "T15 退出码 1" "$rc" "1"
	assert_file_has "T15 RESULT=FAIL" "$sb/out/chk.txt" "RESULT=FAIL"
}

# T16：enter / exit / unlock 的按键与弹窗保护
t16_enter_exit_unlock() {
	sb=$(new_sb t16)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	DPIN_EXIT_WAIT=0.5 "$SHBIN" "$RECOVER" enter >"$sb/out/enter.txt" 2>&1
	assert_eq "T16 enter 退出码 0" "$?" "0"
	assert_file_has "T16 enter 广播" "$sb/out/am.log" "enter_monitor"
	# 没有弹窗标记时：只按 power 键，不做猜测性点击
	DPIN_EXIT_WAIT=0.5 "$SHBIN" "$RECOVER" exit >"$sb/out/exit1.txt" 2>&1
	rc=$?
	assert_eq "T16 exit 无弹窗时返回 1" "$rc" "1"
	assert_file_has "T16 exit 发了 power 键" "$sb/out/input.log" "keyevent 26"
	assert_file_absent "T16 exit 未乱点" "$sb/out/input.log" "tap"
	# 有弹窗标记时：点确认
	printf '  mDpinDialog DpinDialog-dpin_exit_monitor\n' >"$sb/fx/window.txt"
	DPIN_EXIT_WAIT=0.5 "$SHBIN" "$RECOVER" exit >"$sb/out/exit2.txt" 2>&1
	assert_eq "T16 exit 有弹窗时返回 0" "$?" "0"
	assert_file_has "T16 exit 点击确认" "$sb/out/input.log" "tap 1913 1243"
	"$SHBIN" "$RECOVER" unlock >>"$sb/out/exit2.txt" 2>&1
	assert_file_has "T16 unlock 亮屏" "$sb/out/input.log" "keyevent 224"
	assert_file_has "T16 unlock 解锁" "$sb/out/input.log" "keyevent 82"
}

# T17：snapshot 采集齐全
t17_snapshot_collects() {
	sb=$(new_sb t17)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	fixture_broken_a "$sb"
	"$SHBIN" "$RECOVER" snapshot --dir "$sb/out/snap" >"$sb/out/snap.txt" 2>&1
	rc=$?
	assert_eq "T17 退出码 0" "$rc" "0"
	assert_file_has "T17 打印路径" "$sb/out/snap.txt" "SNAPSHOT="
	for f in status.txt logcat-tags.txt props.txt dp-link.txt screen.png; do
		if [ -f "$sb/out/snap/$f" ]; then pass "T17 采集 $f"; else fail "T17 缺 $f"; fi
	done
	assert_file_has "T17 截图非空" "$sb/out/snap/screen.png" "PNG"
}

# T18：allow-restart on/off 只改一个键，其它配置保留
t18_allow_restart_toggle() {
	sb=$(new_sb t18)
	setup_env "$sb"
	"$SHBIN" "$RECOVER" allow-restart on >"$sb/out/a1.txt" 2>&1
	val=$(awk -F= '$1=="ALLOW_FRAMEWORK_RESTART"{print $2}' "$sb/config")
	keep=$(awk -F= '$1=="POLL"{print $2}' "$sb/config")
	assert_eq "T18 打开后为 1" "$val" "1"
	assert_eq "T18 其它键保留" "$keep" "0.05"
	"$SHBIN" "$RECOVER" allow-restart off >"$sb/out/a2.txt" 2>&1
	val2=$(awk -F= '$1=="ALLOW_FRAMEWORK_RESTART"{print $2}' "$sb/config")
	assert_eq "T18 关闭后为 0" "$val2" "0"
	assert_eq "T18 不重复写键" "$(grep -c '^ALLOW_FRAMEWORK_RESTART=' "$sb/config")" "1"
}

# T19：配置解析安全（不做 eval；非法值忽略；未知键忽略）
t19_config_parse_is_safe() {
	sb=$(new_sb t19)
	setup_env "$sb"
	cat >"$sb/config" <<EOF
ENABLE=abc
POLL=\$(touch $sb/out/pwned)
EVIL=1; touch $sb/out/pwned2
ALLOW_FRAMEWORK_RESTART=7
MAX_STARTS_PER_BOOT=3
EOF
	"$SHBIN" "$GUARD" selftest >"$sb/out/self.txt" 2>&1
	assert_eq "T19 selftest 通过" "$?" "0"
	assert_file_has "T19 ENABLE 回默认" "$sb/out/self.txt" "ENABLE=1"
	assert_file_missing "T19 未执行注入命令" "$sb/out/pwned"
	assert_file_missing "T19 未执行未知键注入" "$sb/out/pwned2"
	"$SHBIN" "$RECOVER" framework-restart --yes >"$sb/out/fr.txt" 2>&1
	assert_eq "T19 ALLOW=7 不是 1 → 拒绝" "$?" "1"
}

# T20：host wrapper 的 adb 调用与转义（用假 adb）
t20_host_wrapper_adb_call() {
	sb=$(new_sb t20)
	setup_env "$sb"
	cat >"$sb/bin/fake-adb" <<'EOF'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd)
printf '%s\n' "$*" >>"$SB/out/adb.log"
echo "adb-fake-ok"
EOF
	chmod 755 "$sb/bin/fake-adb"
	SERIAL=ABC123 ADB=fake-adb "$SHBIN" "$HOSTWRAP" status >"$sb/out/host.txt" 2>&1
	rc=$?
	assert_eq "T20 host 退出码 0" "$rc" "0"
	assert_file_has "T20 指定了序列号" "$sb/out/adb.log" "-s ABC123"
	assert_file_has "T20 走 su -c 调设备脚本" "$sb/out/adb.log" "su -c 'sh /data/adb/modules/mipad_dpin_guard/bin/dpin-recover.sh status'"
}

# T21：zip 构建 + 内容 + 权限 + 解包 selftest
t21_zip_build_and_verify() {
	if ! command -v python3 >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then
		fail "T21 缺 python3/unzip，跳过"
		return
	fi
	out=$("$SHBIN" "$FIX/build-guard-zip.sh" 2>&1)
	rc=$?
	assert_eq "T21 构建退出码 0" "$rc" "0"
	assert_contains "T21 有 ZIP 行" "$out" "ZIP="
	assert_contains "T21 有 SHA256" "$out" "SHA256="
	zipf=$(printf '%s\n' "$out" | sed -n 's/^ZIP=//p' | tail -n 1)
	if [ -f "$zipf" ]; then pass "T21 zip 存在"; else fail "T21 zip 不存在"; fi
	list=$(unzip -l "$zipf" 2>/dev/null)
	for need in module.prop service.sh post-fs-data.sh bin/dpin-lib.sh bin/dpin-guard.sh bin/dpin-recover.sh; do
		assert_contains "T21 zip 含 $need" "$list" "$need"
	done
	assert_contains "T21 zip 里脚本可执行" "$(unzip -Z -l "$zipf" 2>/dev/null)" "-rwxr-xr-x"
}

# T22：service.sh 在 ENABLE=0 时不动任何东西；所有脚本 dash -n 通过
t22_service_gate_and_syntax() {
	sb=$(new_sb t22)
	setup_env "$sb"
	printf 'ENABLE=0\n' >"$sb/config"
	DPIN_STATE="$sb/state" "$SHBIN" "$MODDIR/service.sh" >"$sb/out/svc.txt" 2>&1
	assert_eq "T22 ENABLE=0 时 service.sh 退出 0" "$?" "0"
	assert_file_missing "T22 未拉起守护" "$sb/state/daemon.pid"
	for f in "$MODDIR/post-fs-data.sh" "$MODDIR/service.sh" "$MODDIR/uninstall.sh" "$MODDIR"/bin/*.sh "$FIX/install/install-guard.sh" "$FIX/install/uninstall-guard.sh"; do
		if "$SHBIN" -n "$f" 2>"$sb/out/syntax.err"; then pass "T22 dash -n $(basename "$f")"; else fail "T22 dash -n $(basename "$f")：$(cat "$sb/out/syntax.err")"; fi
	done
}


# T23：install/uninstall 离线流程（路径可注入）：备份旧模块、复制、seed config、卸载移出
t23_install_uninstall_offline() {
	sb=$(new_sb t23)
	setup_env "$sb"
	MODROOT="$sb/data/modules/mipad_dpin_guard"
	STATEROOT="$sb/data/mipad_dpin_guard"
	src="$sb/src"
	mkdir -p "$src"
	cp -R "$MODDIR/." "$src/"
	mkdir -p "$MODROOT/old"
	printf 'old-marker\n' >"$MODROOT/old/marker"
	DPIN_MOD_DIR="$MODROOT" DPIN_STATE_ROOT="$STATEROOT" "$SHBIN" "$FIX/install/install-guard.sh" "$src" >"$sb/out/inst.txt" 2>&1
	rc=$?
	assert_eq "T23 安装退出码 0" "$rc" "0"
	assert_dir "T23 模块已就位" "$MODROOT/bin"
	if [ -f "$MODROOT/module.prop" ] && [ -f "$MODROOT/service.sh" ]; then pass "T23 载荷齐全"; else fail "T23 载荷缺失"; fi
	bak=$(ls -d "$STATEROOT"/backup-* 2>/dev/null | head -n 1)
	if [ -n "$bak" ] && [ -f "$bak/old/marker" ]; then pass "T23 旧模块已备份（未删除）"; else fail "T23 旧模块未备份"; fi
	if [ -f "$STATEROOT/config" ]; then pass "T23 config 已 seed"; else fail "T23 config 未 seed"; fi
	assert_file_has "T23 打印回退指引" "$sb/out/inst.txt" "回退:"
	# 卸载：模块目录应被移出到 state/removed-*，而不是删除
	DPIN_MOD_DIR="$MODROOT" DPIN_STATE_ROOT="$STATEROOT" "$SHBIN" "$FIX/install/uninstall-guard.sh" >"$sb/out/uninst.txt" 2>&1
	assert_eq "T23 卸载退出码 0" "$?" "0"
	if [ -d "$MODROOT" ]; then fail "T23 模块目录仍在（应被移出）"; else pass "T23 模块目录已移出"; fi
	rmv=$(ls -d "$STATEROOT"/removed-* 2>/dev/null | head -n 1)
	if [ -n "$rmv" ] && [ -f "$rmv/module.prop" ]; then pass "T23 卸载产物可回滚"; else fail "T23 卸载产物缺失"; fi
}

# T24：DfMS 已收到但 App 没有 params → BROKEN_C
t24_status_broken_c() {
	sb=$(new_sb t24)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=3408, height=2272, framerate=120, data_format=0, color_depth=8, status=0
1763071966.060  24032 24032 I DisplayFeatureManagerService: receive dp in info: width=3408, height=2272, framerate=120, status=0
1763071966.061  24032 24032 I DisplayFeatureManagerService: notifyDpInCallback: width=3408, height=2272, framerate=120, status=0
EOF
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	assert_eq "T24 退出码 2" "$?" "2"
	assert_file_has "T24 判定 BROKEN_C" "$sb/out/status.txt" "CLASS=BROKEN_C"
}

# T25：链路通但没 startCaptureSession → OK_NO_CAPTURE（不误报 OK）
t25_status_ok_no_capture() {
	sb=$(new_sb t25)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=3408, height=2272, framerate=120, data_format=0, color_depth=8, status=0
1763071966.060  24032 24032 I DisplayFeatureManagerService: receive dp in info: width=3408, height=2272, framerate=120, status=0
1763071966.070  24032 24032 I DPIN    : AppLifecycleManager Display params changed: width=3408, height=2272, frameRate=120, dataFormat=0, colorDepth=8, status=0
EOF
	"$SHBIN" "$RECOVER" status >"$sb/out/status.txt" 2>&1
	assert_eq "T25 退出码 5" "$?" "5"
	assert_file_has "T25 判定 OK_NO_CAPTURE" "$sb/out/status.txt" "CLASS=OK_NO_CAPTURE"
}

# T26：recover 对 BROKEN_B 只给"物理拔插"，不做软件尝试
t26_recover_broken_b_is_honest() {
	sb=$(new_sb t26)
	setup_env "$sb"
	fixture_live_procs "$sb" 3000 4000 >/dev/null
	cat >"$sb/fx/logcat.txt" <<'EOF'
1763071966.050  24032 24032 I displaycount: Received DP-IN: width=0, height=0, framerate=0, data_format=0, color_depth=0, status=1
EOF
	"$SHBIN" "$RECOVER" recover >"$sb/out/rec.txt" 2>&1
	assert_eq "T26 退出码 3" "$?" "3"
	assert_file_has "T26 要求物理拔插" "$sb/out/rec.txt" "物理拔插"
	assert_file_missing "T26 未执行 kill" "$sb/out/kill.log"
	assert_file_missing "T26 未碰 UDP/UDC" "$sb/out/setprop.log"
}

# T27：服务已在 servicemanager 可见但 /proc 扫不到 → 不重复 ctl.start（幂等保护）
t27_guard_no_restart_when_published() {
	sb=$(new_sb t27)
	setup_env "$sb"
	printf '3000\n' >"$sb/fx/pidof_system_server"
	mkproc "$sb" 3000 system_server /system/bin/system_server 500
	printf '1\n' >"$sb/fx/hal_starts_left"
	printf '0 vendor.xiaomi.hardware.displayfeature_aidl.IDisplayFeature/default: []\n' >"$sb/fx/services.txt"
	DPIN_DAEMON_MAX_LOOPS=3 "$SHBIN" "$GUARD" daemon >"$sb/out/daemon.out" 2>&1
	assert_file_missing "T27 未重复 ctl.start" "$sb/out/setprop.log"
	assert_file_has "T27 记录服务已发布" "$sb/state/events.log" "服务已发布"
}

# ================================================================ 执行

run_case t01_guard_pulls_hal_at_framework_start
run_case t02_guard_noop_when_hal_present
run_case t03_guard_start_failure_is_bounded
run_case t04_guard_warns_on_hal_restart_after_framework
run_case t05_status_detects_broken_a
run_case t06_status_detects_ok
run_case t07_status_detects_broken_b
run_case t08_status_no_dpin
run_case t09_stale_receive_does_not_count
run_case t10_recover_refuses_without_optin
run_case t11_framework_restart_requires_yes
run_case t12_framework_restart_happy_path
run_case t13_framework_restart_once_per_boot
run_case t14_check_chain_pass
run_case t15_check_chain_fail
run_case t16_enter_exit_unlock
run_case t17_snapshot_collects
run_case t18_allow_restart_toggle
run_case t19_config_parse_is_safe
run_case t20_host_wrapper_adb_call
run_case t21_zip_build_and_verify
run_case t22_service_gate_and_syntax
run_case t23_install_uninstall_offline
run_case t24_status_broken_c
run_case t25_status_ok_no_capture
run_case t26_recover_broken_b_is_honest
t28_install_resets_dangerous_switch() {
	sb=$(new_sb t28)
	setup_env "$sb"
	MODROOT="$sb/data/modules/mipad_dpin_guard"
	STATEROOT="$sb/data/mipad_dpin_guard"
	mkdir -p "$STATEROOT"
	printf 'ENABLE=1\nALLOW_FRAMEWORK_RESTART=1\n' >"$STATEROOT/config"
	src="$sb/src"
	mkdir -p "$src"
	cp -R "$MODDIR/." "$src/"
	DPIN_MOD_DIR="$MODROOT" DPIN_STATE_ROOT="$STATEROOT" "$SHBIN" "$FIX/install/install-guard.sh" "$src" >"$sb/out/inst.txt" 2>&1
	assert_eq "T28 安装退出码 0" "$?" "0"
	assert_contains "T28 复位提示" "$(cat "$sb/out/inst.txt")" "复位为 0"
	if grep -q '^ALLOW_FRAMEWORK_RESTART=0' "$STATEROOT/config"; then pass "T28 危险开关已复位"; else fail "T28 危险开关未复位"; fi
	bak=$(ls "$STATEROOT"/config.bak-* 2>/dev/null | head -n 1)
	if [ -n "$bak" ] && grep -q '^ALLOW_FRAMEWORK_RESTART=1' "$bak"; then pass "T28 旧 config 留档"; else fail "T28 旧 config 未留档"; fi
}
run_case t28_install_resets_dangerous_switch
run_case t27_guard_no_restart_when_published

printf '\n== 汇总 ==\n'
printf 'PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
	printf '失败项:%s\n' "$FAILED"
	printf 'sandbox 保留在: %s\n' "$ROOT"
	exit 1
fi
printf 'sandbox: %s\n' "$ROOT"
exit 0
