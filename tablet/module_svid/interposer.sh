#!/system/bin/sh
# interposer：patched HAL + DSO 的布置与核对，被 service.sh / action.sh source。
# 约定：调用方已进入 init mount namespace，已 source profile.sh，$MODDIR 已设置。
HAL=/odm/bin/vendor.xiaomi.usb.dp-service
DSO_TARGET=/odm/lib64/libanc_supermoon.so
LIBDIR=/mnt/vendor/mipad_lib
PATCH="$MODDIR/bin/hal-patch"
DSO="$MODDIR/lib/fullsplit-v4.so"

_i_say() { echo "interposer: $*"; }
_i_ident() { stat -c '%d:%i' "$1" 2>/dev/null; }
_i_mounted() { grep -q " $1 " /proc/self/mountinfo 2>/dev/null; }
_i_sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
# 同一个文件：先比 inode；tmpfs 重建过时 inode 会变，再比内容。
_i_ours() {
 [ -f "$1" ] && [ -f "$2" ] || return 1
 [ "$(_i_ident "$1")" = "$(_i_ident "$2")" ] && return 0
 [ -n "$(_i_sha "$1")" ] && [ "$(_i_sha "$1")" = "$(_i_sha "$2")" ]
}
# 目标上的挂载是不是“上一代本模块”的：源是 tmpfs 且根是 /dso.so 或 /hal-patched。
_i_stale_ours() {
 local info
 info=$(awk -v t="$1" '$5==t {for(i=1;i<=NF;i++) if($i=="-"){print $(i+1), $4; exit}}' \
  /proc/self/mountinfo 2>/dev/null)
 [ "$info" = "tmpfs $2" ]
}
# 有进程映射的 inode 是否就是“当前目标 inode”（= 原厂库）。
# 映射旧 DSO 的孤儿 inode 不算：重新 bind 不影响已运行进程。
_i_maps_original() {
 local ino
 ino=$(stat -c %i "$DSO_TARGET" 2>/dev/null) || return 1
 [ -n "$ino" ] || return 1
 awk -v t="$DSO_TARGET" -v i="$ino" '$6==t && $5==i {f=1} END{exit !f}' \
  /proc/[0-9]*/maps 2>/dev/null
}

# HAL 进程状态：stopped / stock:<pid>（原厂镜像）/ active:<pid>（已加载 DSO）
interposer_hal_state() {
 p=$(pidof vendor.xiaomi.usb.dp-service 2>/dev/null)
 [ -n "$p" ] || { echo stopped; return; }
 if grep -q libanc "/proc/$p/maps" 2>/dev/null; then echo "active:$p"; else echo "stock:$p"; fi
}

# 返回：0=已生效 1=拒绝/失败 2=已布置但需等 HAL 自然重载 3=无法热应用（原厂库正被映射）
interposer_reconcile() {
 local dso hal
 [ -e "$DSO_TARGET" ] || { _i_say "目标库不存在"; return 1; }
 [ -x "$PATCH" ] || { _i_say "缺少 hal-patch"; return 1; }
 [ -f "$DSO" ] || { _i_say "缺少 DSO"; return 1; }

 # ---- DSO：ours / 上一代（内容不同则在上面叠一层新 bind）/ 外国 ----
 dso=0
 if _i_mounted "$DSO_TARGET"; then
  if _i_ours "$DSO_TARGET" "$LIBDIR/dso.so" || [ "$(_i_sha "$DSO_TARGET")" = "$(_i_sha "$DSO")" ]; then
   dso=1
  elif _i_stale_ours "$DSO_TARGET" /dso.so; then
   _i_say "沿用上一代 DSO 挂载（内容不同，将叠加新 bind）"
   dso=2
  else
   _i_say "DSO 目标已被非本模块挂载，拒绝覆盖"; return 1
  fi
 fi
 # ---- HAL：ours / 上一代 / 外国 ----
 hal=0
 if _i_mounted "$HAL"; then
  if _i_ours "$HAL" "$LIBDIR/hal-patched"; then
   hal=1
  elif _i_stale_ours "$HAL" /hal-patched; then
   _i_say "沿用上一代 HAL 挂载"
   hal=1
  else
   _i_say "HAL 目标已被非本模块挂载，拒绝覆盖"; return 1
  fi
 fi
 if [ "$dso" = 1 ] && [ "$hal" = 1 ]; then
  _i_say "已布置"
  case "$(interposer_hal_state)" in active:*) return 0 ;; *) return 2 ;; esac
 fi

 # 要新建 DSO bind：只有“当前原厂库 inode”正被映射时才等（旧 DSO 的孤儿 inode 不算）
 if [ "$dso" = 0 ] && _i_maps_original; then
  _i_say "原厂库正被进程映射，本次不覆盖"
  return 3
 fi

 mkdir -p "$LIBDIR" || return 1
 mountpoint -q "$LIBDIR" || mount -t tmpfs -o size=16m tmpfs "$LIBDIR" || return 1
 cp "$DSO" "$LIBDIR/dso.so" && chmod 644 "$LIBDIR/dso.so" &&
  chcon u:object_r:vendor_file:s0 "$LIBDIR/dso.so" || return 1

 if [ "$hal" = 0 ]; then
  # $HAL 是原厂镜像，patch 幂等
  "$PATCH" "$HAL" "$LIBDIR/hal-patched" "$DSO_TARGET" || return 1
  chmod 755 "$LIBDIR/hal-patched"
  chcon u:object_r:hal_usb_dp_relay_default_exec:s0 "$LIBDIR/hal-patched" || return 1
 fi

 if [ "$dso" != 1 ]; then
  mount --bind "$LIBDIR/dso.so" "$DSO_TARGET" || return 1
 fi
 if [ "$hal" = 0 ]; then
  if ! mount --bind "$LIBDIR/hal-patched" "$HAL"; then
   [ "$dso" = 0 ] && umount "$DSO_TARGET" 2>/dev/null
   return 1
  fi
 fi
 _i_say "已布置"
 case "$(interposer_hal_state)" in active:*) return 0 ;; *) return 2 ;; esac
}

interposer_report() {
 echo "SVID: $([ -n "$(awk -v t="$TARGET" '$5==t {print $1}' /proc/self/mountinfo 2>/dev/null)" ] && cat "$TARGET" 2>/dev/null || echo 未挂载)"
 echo "HAL bind: $(_i_mounted "$HAL" && echo yes || echo no)"
 echo "DSO bind: $(_i_mounted "$DSO_TARGET" && echo yes || echo no)"
 echo "HAL: $(interposer_hal_state)"
}
