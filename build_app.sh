#!/bin/bash
# 打包 "Mipad DP Touch.app" —— 打开就是菜单栏图标, 不需要终端。
#
# 为什么用 PyInstaller 而不是 py2app:
#   py2app 假设 Python 是标准 framework 布局 (/Library/Frameworks/Python.framework)。
#   本机只有 CommandLineTools 版 (/Library/Developer/CommandLineTools/.../Python3.framework),
#   相对路径深度不同, 打出来的 app 启动时 dyld 直接报
#   "Library not loaded: @executable_path/../../../../Python3"。
#   PyInstaller 自己管解释器和动态库, 两种布局都能出可运行的 bundle。
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
APP_NAME="Mipad DP Touch"
# bundle id 保持稳定 (改名字不改 id): TCC 的输入监控/辅助功能授权是按 id 匹配的,
# 换了 id 用户就得重新授权一遍。
BUNDLE_ID="io.github.inyvn.xiaomi-dptouch"
VER="2.2.4"
ICON="AppIcon.icns"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> 创建构建环境 $VENV"
  # 找 uv: 不能只靠 command -v —— 从 GUI / 后台任务拉起的 shell 里 PATH 可能不含
  # ~/.local/bin 或 ~/.hermes/bin, 于是会静悄悄落到系统 /usr/bin/python3 (=3.9)。
  UV=""
  for c in uv "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.hermes/bin/uv"; do
    if command -v "$c" >/dev/null 2>&1; then UV="$(command -v "$c")"; break; fi
  done
  PY3=""
  for c in python3.12 python3.13 python3.11; do
    if command -v "$c" >/dev/null 2>&1; then PY3="$(command -v "$c")"; break; fi
  done
  if [ -n "$UV" ]; then
    "$UV" venv --python 3.12 "$VENV"
    "$UV" pip install --python "$VENV/bin/python" -q pyobjc-framework-Cocoa \
      pyobjc-framework-ApplicationServices pyobjc-framework-ServiceManagement pyinstaller
  elif [ -n "$PY3" ]; then
    "$PY3" -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q pyobjc-framework-Cocoa \
      pyobjc-framework-ApplicationServices pyobjc-framework-ServiceManagement pyinstaller
  else
    echo "!! 找不到 uv, 也没有 python3.11+。装一个:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
fi

# 解释器必须 >= 3.11: 系统 python3 是 3.9, pyobjc 12 / PyInstaller 6 在它上面打出来的
# bundle 与实测版本不是同一套, 别用。
PYV="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYV" in
  3.9|3.10|2.*)
    echo "!! $VENV 里是 Python $PYV, 太老 (要 3.11+)。删掉它再跑一次:  rm -rf $VENV" >&2
    exit 1 ;;
esac
echo "    构建解释器: Python $PYV"

echo "==> 语法检查"
"$VENV/bin/python" -m py_compile src/dptouch.py src/dptouch_engine.py src/dptouch_display.py \
  src/dptouch_mode.py src/dptouch_edid.py src/dptouch_calibration.py src/dptouch_window.py src/dptouch_touchviz.py src/hid_bridge.py src/dptouch_autostart.py src/dptouch_update.py src/dptouch_plugin.py

# 原生 EDID 助手: 必须编译成功且自检通过, 否则直接失败 —— 不能静默吞掉。
# 助手派生自 github.com/arlint/vedid (MIT, tools/native/LICENSE), 职责:
# guard 模式下持有原生 IOAV 目标句柄, 父进程 stdin EOF/崩溃/信号时经原句柄恢复。
echo "==> 编译原生 EDID 助手 (vedid)"
NATIVE_OUT=build/native
rm -rf "$NATIVE_OUT"
mkdir -p "$NATIVE_OUT"
if ! clang -fobjc-arc -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -Wall -O2 \
    -framework Foundation -framework AppKit -framework CoreGraphics -framework IOKit \
    -F/System/Library/PrivateFrameworks -framework CoreDisplay \
    tools/native/vedid.m -o "$NATIVE_OUT/vedid" 2>&1 | tail -5; then
  echo "!! vedid 编译失败 (见上方 clang 输出)" >&2
  exit 1
fi
echo "==> 助手签名/自检 (失败即停, 不打一个吞掉失败的包)"
if ! "$NATIVE_OUT/vedid" selftest; then
  echo "!! vedid selftest 失败" >&2
  exit 1
fi
# 384B EDID 校验路径也当场验一遍 (面板 EDID = 基础块 + 2 扩展)
if ! "$NATIVE_OUT/vedid" validate --edid tools/edid-ab-evidence/ab-1793854557/mi-display-original.edid; then
  echo "!! vedid 拒绝 384B 面板 EDID (multiples128/扩展数/校验和校验回退)" >&2
  exit 1
fi

# 模块必须能真正 import 再打 (含新加的 dptouch_edid) —— 打完了才炸就太晚了
echo "==> import 检查 (打之前先能导入)"
"$VENV/bin/python" - <<'PYEOF'
import sys
sys.path.insert(0, "src")
import dptouch, dptouch_edid, dptouch_calibration, dptouch_engine, dptouch_display, dptouch_window
import dptouch_autostart, dptouch_update, dptouch_mode, hid_bridge
import dptouch_edid as ED
# 模块能导入 + 纯逻辑可用 (不碰显示器): 非法 EDID 必须被拒
try:
    ED.validate(b"\x00" * 128)
    print("!! dptouch_edid.validate 接受了垃圾 EDID")
    sys.exit(1)
except ED.EDIDError:
    pass
print("    import ok: 7 个模块 + dptouch_edid 纯逻辑可用")
PYEOF

echo "==> 清理"
rm -rf build-pyi dist-pyi

echo "==> 图标 (logo.png -> $ICON)"
ICON_ARG=()
if [ -f logo.png ]; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  # 从原图缩放生成全套尺寸 (原图建议 >= 1024x1024 且带透明通道)
  for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
              "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" "512 icon_256x256@2x" \
              "512 icon_512x512" "1024 icon_512x512@2x"; do
    set -- $spec
    sips -z "$1" "$1" logo.png --out "$ICONSET/$2.png" >/dev/null 2>&1
  done
  if iconutil -c icns "$ICONSET" -o "$ICON" 2>/dev/null; then
    echo "    $ICON  $(du -h "$ICON" | cut -f1)"
  else
    echo "    !! 生成失败, 本次不带图标"
  fi
  rm -rf "$(dirname "$ICONSET")"
fi
if [ -f "$ICON" ]; then ICON_ARG=(--icon "$PWD/$ICON"); else echo "    (没有 $ICON, 用默认图标)"; fi

echo "==> PyInstaller 打包"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --windowed \
  --name "MipadDPTouch" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  "${ICON_ARG[@]}" \
  --hidden-import ApplicationServices --hidden-import objc \
  --hidden-import AppKit --hidden-import Foundation \
  --hidden-import ServiceManagement \
  --hidden-import dptouch_autostart \
  --hidden-import dptouch_update \
  --hidden-import dptouch_edid \
  --hidden-import dptouch_mode \
  --hidden-import dptouch_plugin \
  --hidden-import dptouch_touchviz \
  --hidden-import plugin_icons \
  --hidden-import ssl --hidden-import _ssl --hidden-import _hashlib \
  --paths src \
  --add-data "$PWD/docs/logo.png:." \
  --add-binary "$PWD/$NATIVE_OUT/vedid:." \
  --distpath dist-pyi --workpath build-pyi --specpath build-pyi \
  src/dptouch.py 2>&1 | tail -4

BUILT="dist-pyi/MipadDPTouch.app"
[ -d "$BUILT" ] || { echo "!! 打包失败: $BUILT 不存在" >&2; exit 1; }

# 助手必须真进了 bundle, 且是能跑的二进制 (签名后要能再执行 selftest)
HELPER_IN_BUNDLE="$(cd "$BUILT/Contents" 2>/dev/null && find . -name vedid -type f | head -1 | sed 's#^\./##')"
if [ -z "$HELPER_IN_BUNDLE" ]; then
  echo "!! vedid 没打进 bundle" >&2
  exit 1
fi
echo "    助手已进 bundle: Contents/$HELPER_IN_BUNDLE"
"$BUILT/Contents/$HELPER_IN_BUNDLE" selftest >/dev/null 2>&1 \
  || { echo "!! bundle 里的 vedid 跑不起来" >&2; exit 1; }
echo "    bundle 里的 vedid selftest: 通过"

echo "==> 调整 bundle 信息"
PL="$BUILT/Contents/Info.plist"
# 关键: PyInstaller 已经写过 CFBundleName / CFBundleDisplayName / 版本号这几个键,
# 对已存在的键用 Add 会失败并静默留下 PyInstaller 的默认值 —— 曾经因此让展示名
# 停在 "MipadDPTouch"、版本停在 "0.0.0"。所以一律先 Set, 失败再 Add。
plist_str() {   # plist_str <key> <value>
  /usr/libexec/PlistBuddy -c "Set :$1 '$2'" "$PL" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :$1 string '$2'" "$PL" 2>/dev/null || true
}
plist_raw() {   # plist_raw <key> <type> <value>   (bool/integer 不要引号)
  /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PL" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PL" 2>/dev/null || true
}
plist_str CFBundleName "$APP_NAME"
plist_str CFBundleDisplayName "$APP_NAME"
plist_str CFBundleShortVersionString "$VER"
plist_str CFBundleVersion "$VER"
# 菜单栏应用: 不进 Dock、不抢前台
plist_raw LSUIElement bool true
plist_str LSMinimumSystemVersion "11.0"
plist_raw NSHighResolutionCapable bool true

# 图标: PyInstaller 会把 .icns 拷进 Resources; 这里确保 plist 指向它
# (Finder / 系统设置 / 登录项里显示的就是这个图标)
ICNS_IN_BUNDLE="$(cd "$BUILT/Contents/Resources" 2>/dev/null && ls *.icns 2>/dev/null | head -1)"
if [ -n "$ICNS_IN_BUNDLE" ]; then
  NAME_NOEXT="${ICNS_IN_BUNDLE%.icns}"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile '$NAME_NOEXT'" "$PL" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string '$NAME_NOEXT'" "$PL" 2>/dev/null || true
  echo "    图标: Contents/Resources/$ICNS_IN_BUNDLE"
else
  echo "    !! bundle 里没有 .icns (图标没打进去)"
fi

rm -rf "dist-pyi/$APP_NAME.app"
mv "$BUILT" "dist-pyi/$APP_NAME.app"

echo "==> ad-hoc 签名"
# 1) --deep 先把嵌套的 .so/.dylib 签上
codesign --force --deep --sign - "dist-pyi/$APP_NAME.app"

# 2) 外层再补一条「稳定指定要求」。
#    ad-hoc 签名的默认指定要求是 cdhash H"..."; 重新打包 cdhash 必变
#    → 系统里已授权的 TCC 记录对不上（tccd 日志会写
#      "Failed to match existing code requirement ... kTCCServiceAccessibility")
#    → App 看起来有权限、实际每次请求都被拒。
#    改成只认 bundle id 后，授权可跨重新打包保留（一次授权，长期有效）。
#    注意: 这条要求不能与 --deep 连用 —— --deep 会把它套到嵌套二进制上，
#    导致 "nested code is modified or invalid"（实测 20 个文件校验失败）。
BID="$BUNDLE_ID"
if [ -n "$BID" ]; then
  codesign --force --sign - -r="designated => identifier \"$BID\"" "dist-pyi/$APP_NAME.app"
fi

codesign --verify --deep --strict "dist-pyi/$APP_NAME.app"
codesign -dv "dist-pyi/$APP_NAME.app" 2>&1 | grep -E 'Identifier|Signature' || true
codesign -d -r- "dist-pyi/$APP_NAME.app" 2>&1 | grep -i designated || true

echo
echo "==> 完成: dist-pyi/$APP_NAME.app"
du -sh "dist-pyi/$APP_NAME.app"
echo
echo "安装:  cp -R \"dist-pyi/$APP_NAME.app\" /Applications/ && open \"/Applications/$APP_NAME.app\""
