#!/usr/bin/env bash
# 打一个能直接发给用户的 DMG：里面是 app + 「应用程序」快捷方式 + 首次打开说明。
# 用法： ./make_dmg.sh          （需要先跑过 ./build_app.sh）
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Mipad DP Touch"
APP="dist-pyi/${APP_NAME}.app"
[ -d "$APP" ] || { echo "==> 没找到 $APP ，先跑 ./build_app.sh"; exit 1; }

VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null || echo "unknown")
OUT="dist-pyi/Mipad-DP-Touch-${VER}.dmg"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

echo "==> 准备内容"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/应用程序"

cat > "$STAGE/首次打开请看这里.txt" <<'TXT'
首次打开
========

1. 把左边的「Mipad DP Touch」拖进右边的「应用程序」。
2. 从「应用程序」里双击它。如果系统提示「Apple 无法检查它是否包含恶意软件」
   或者「已损坏」——这是本工具没有做 Apple 公证（需要付费开发者账号）导致的，不是文件坏了。
   任选一种方式放行：

   方式一（推荐）：
     打开「系统设置 → 隐私与安全性」，在「安全性」一栏找到被拦下的那条，
     点「仍要打开」，再确认一次。

   方式二（终端一条命令）：
     打开「终端」，粘贴下面这行回车：
       xattr -dr com.apple.quarantine "/Applications/Mipad DP Touch.app"

3. 打开后会有一个 ✎ 图标出现在菜单栏，并弹出设置窗口，按提示分两步授权：
   ① 辅助功能 → ② 输入监控。两项都 ✓ 后点「立即重启」。

装完想卸载：退出 App，把「应用程序」里的它删掉即可。
TXT

echo "==> 生成 DMG（压缩格式，可能要十几秒）"
rm -f "$OUT"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null

echo "==> 自检：挂载 DMG，看看里面到底是什么"
MNT=$(mktemp -d)
hdiutil attach "$OUT" -nobrowse -readonly -mountpoint "$MNT" >/dev/null
ls -l "$MNT"
codesign -dv "$MNT/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature" || true
xattr -w com.apple.quarantine test "$MNT/${APP_NAME}.app" 2>/dev/null || echo "（只读挂载，跳过隔离属性测试）"
hdiutil detach "$MNT" >/dev/null
rmdir "$MNT"

echo
echo "==> 完成: $OUT"
echo "    SHA256: $(shasum -a 256 "$OUT" | awk '{print $1}')"
echo "    大小:   $(du -h "$OUT" | cut -f1)"
