#!/bin/sh
# 用 Android NDK 编译平板侧 LD_PRELOAD 门禁 DSO（模块里的 fullsplit-bounded-v2.so）。
#
#   用法:
#     tablet/build_dso.sh [输出路径]
#     ANDROID_NDK=/path/to/ndk tablet/build_dso.sh /tmp/fullsplit-bounded-v2.so
#
#   NDK 查找顺序：$ANDROID_NDK → $ANDROID_NDK_HOME → $ANDROID_NDK_ROOT →
#   $ANDROID_HOME/ndk/<最新版本> → $ANDROID_SDK_ROOT/ndk/<最新版本> →
#   <仓库>/.toolchain/ndk（离线工具链，可自行解压）。
#
#   默认输出到 tablet/native/build/fullsplit-bounded-v2.so；脚本不会改动
#   tablet/module/ 里的模块载荷，换不换由打包步骤决定。
#
#   实验分支（native-touch lab）:
#     MIPAD_DSO_LAB=1 tablet/build_dso.sh /tmp/fullsplit-lab.so
#   额外编译 fullsplit-lab.h 的 probe/variant/no-grab 钩子（导出 ioctl、
#   openat），但所有钩子在 candidate 环境没有 MIPAD_NATIVE_LAB=1 时都按
#   syscall 原样透传；不设 MIPAD_DSO_LAB 时产物与生产 DSO 完全相同。
#
# 编译参数刻意与 2.2.0-beta 里那份 .so 对齐：NDK r27c（clang 18.0.3 / LLD 18.0.3）、
# API 35、-O2、-shared -fPIC、FULLSPLIT_EXPLICIT=1、RX_TARGET_PATH=/dev/hidg1。
# 不加 -s / --strip-all：保留 .symtab 与 8 个导出符号（write/close/open/dup2/dup3/…），
# 与旧 .so 一样是 "not stripped"，host 侧排障与符号比对都还要用。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/native/fullsplit-bounded-interposer.c"
OUT=${1:-$HERE/native/build/fullsplit-bounded-v2.so}
LAB=${MIPAD_DSO_LAB:-0}

if [ ! -f "$SRC" ]; then
	echo "找不到源码: $SRC" >&2
	exit 1
fi

find_ndk() {
	if [ -n "${ANDROID_NDK:-}" ]; then echo "$ANDROID_NDK"; return 0; fi
	if [ -n "${ANDROID_NDK_HOME:-}" ]; then echo "$ANDROID_NDK_HOME"; return 0; fi
	if [ -n "${ANDROID_NDK_ROOT:-}" ]; then echo "$ANDROID_NDK_ROOT"; return 0; fi
	for sdk in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}"; do
		[ -n "$sdk" ] && [ -d "$sdk/ndk" ] || continue
		latest=$(ls -1 "$sdk/ndk" 2>/dev/null | sort -V | tail -1)
		[ -n "$latest" ] && { echo "$sdk/ndk/$latest"; return 0; }
	done
	for cand in "$HERE"/../.toolchain/ndk "$HERE"/../.toolchain/ndk-*; do
		[ -d "$cand" ] && { echo "$cand"; return 0; }
	done
	return 1
}

NDK=$(find_ndk) || {
	echo "找不到 Android NDK：设置 ANDROID_NDK，或把 NDK 解压到 <仓库>/.toolchain/ndk" >&2
	exit 1
}

case "$(uname -s)" in
Darwin) HOST_TAG=darwin-x86_64 ;;
Linux) HOST_TAG=linux-x86_64 ;;
*) HOST_TAG=$(ls -1 "$NDK/toolchains/llvm/prebuilt" 2>/dev/null | head -1) ;;
esac
BIN="$NDK/toolchains/llvm/prebuilt/$HOST_TAG/bin"
CC="$BIN/aarch64-linux-android35-clang"
[ -x "$CC" ] || CC=$(ls -1 "$BIN"/aarch64-linux-android*-clang 2>/dev/null | sort -V | tail -1)
[ -x "$CC" ] || {
	echo "NDK 里找不到 aarch64-linux-android35-clang（$BIN）" >&2
	exit 1
}

mkdir -p "$(dirname "$OUT")"
echo "NDK : $NDK"
echo "CC  : $CC"
LAB_DEFS=
if [ "$LAB" = 1 ]; then LAB_DEFS='-DFULLSPLIT_LAB=1'; fi
"$CC" --target=aarch64-linux-android35 -shared -fPIC -O2 -pthread $LAB_DEFS \
	-DFULLSPLIT_EXPLICIT=1 '-DRX_TARGET_PATH="/dev/hidg1"' \
	-o "$OUT" "$SRC" -llog

# 自检：导出符号集合、动态依赖与 "not stripped" 必须和旧 .so 一致，否则模块装载后
# 拦截不到 write/close/open/dup*。
if [ -x "$BIN/llvm-nm" ]; then
	got=$("$BIN/llvm-nm" -D --defined-only "$OUT" | awk 'NF==3{print $3}' | LC_ALL=C sort | tr '\n' ' ')
	if [ "$LAB" = 1 ]; then
		want='__open_2 __write_chk close dup2 dup3 ioctl open open64 openat write '
	else
		want='__open_2 __write_chk close dup2 dup3 open open64 write '
	fi
	if [ "$got" != "$want" ]; then
		echo "导出符号不符合预期: $got" >&2
		exit 1
	fi
	"$BIN/llvm-objdump" -h "$OUT" | grep -q '\.symtab' || {
		echo "生成的 .so 被 strip 了，符号表缺失" >&2
		exit 1
	}
	needed=$("$BIN/llvm-objdump" -p "$OUT" | awk '/NEEDED/{print $2}' | LC_ALL=C sort | tr '\n' ' ')
	want_needed='libc.so libdl.so liblog.so '
	if [ "$needed" != "$want_needed" ]; then
		echo "动态依赖不符合预期: $needed" >&2
		exit 1
	fi
fi

echo "已编译: $OUT"
ls -l "$OUT" | awk '{printf "  大小: %d 字节\n", $5}'
if command -v shasum >/dev/null 2>&1; then
	echo "  SHA256: $(shasum -a 256 "$OUT" | awk '{print $1}')"
else
	echo "  SHA256: $(sha256sum "$OUT" | awk '{print $1}')"
fi
if [ "$LAB" = 1 ]; then
	echo "  导出符号: write close open open64 openat ioctl __open_2 __write_chk dup2 dup3（lab, not stripped）"
else
	echo "  导出符号: write close open open64 __open_2 __write_chk dup2 dup3（not stripped）"
fi
