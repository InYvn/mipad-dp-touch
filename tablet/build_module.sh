#!/bin/sh
# 打包平板侧模块：tablet/module/ -> tablet/dist/mipad-dp-touch-module-<版本>.zip
#
# 产出的 zip 是标准 Magisk 模块格式（module.prop 在根 + META-INF 安装器），
# KernelSU 与 Magisk 的管理器都能直接装。里面不含任何小米二进制，只有几个 shell 脚本。
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/module"
VER=$(sed -n 's/^version=//p' "$SRC/module.prop" | head -1)
[ -n "$VER" ] || { echo "读不到版本号: $SRC/module.prop"; exit 1; }
OUT="$HERE/dist"
NAME="mipad-dp-touch-module-${VER}.zip"

mkdir -p "$OUT"
cd "$SRC"
chmod 755 ./*.sh META-INF/com/google/android/update-binary
rm -f "$OUT/$NAME"
zip -qr9 "$OUT/$NAME" . -x '.*' -x '__MACOSX/*' -x '*.DS_Store' -x 'dist/*'

echo "已打包: $OUT/$NAME"
ls -l "$OUT/$NAME" | awk '{printf "  大小: %.1f KB\n", $5/1024}'
echo "  SHA256: $(shasum -a 256 "$OUT/$NAME" | cut -d' ' -f1)"
echo
echo "装法（平板已 root）:"
echo "  · 管理器里：KernelSU / Magisk -> 模块 -> 从存储安装 -> 选这个 zip -> 重启"
echo "  · 命令行  : su -c 'ksud module install /sdcard/Download/$NAME'   # KernelSU"
