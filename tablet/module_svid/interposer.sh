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
_i_ours() { [ -n "$(_i_ident "$1")" ] && [ "$(_i_ident "$1")" = "$(_i_ident "$2")" ]; }

# HAL 进程状态：stopped / stock:<pid>（原厂镜像）/ active:<pid>（已加载 DSO）
interposer_hal_state() {
 p=$(pidof vendor.xiaomi.usb.dp-service 2>/dev/null)
 [ -n "$p" ] || { echo stopped; return; }
 if grep -q libanc "/proc/$p/maps" 2>/dev/null; then echo "active:$p"; else echo "stock:$p"; fi
}

# 返回：0=已布置 1=拒绝/失败 2=已布置但需等 HAL 自然重载 3=无法热应用（有进程映射原厂库）
interposer_reconcile() {
 local dso hal
 [ -e "$DSO_TARGET" ] || { _i_say "目标库不存在"; return 1; }
 [ -x "$PATCH" ] || { _i_say "缺少 hal-patch"; return 1; }
 [ -f "$DSO" ] || { _i_say "缺少 DSO"; return 1; }

 if _i_mounted "$DSO_TARGET"; then
  _i_ours "$DSO_TARGET" "$LIBDIR/dso.so" || { _i_say "DSO 目标已被非本模块挂载，拒绝覆盖"; return 1; }
  dso=1
 else
  dso=0
 fi
 if _i_mounted "$HAL"; then
  _i_ours "$HAL" "$LIBDIR/hal-patched" || { _i_say "HAL 目标已被非本模块挂载，拒绝覆盖"; return 1; }
  hal=1
 else
  hal=0
 fi
 if [ "$dso" = 1 ] && [ "$hal" = 1 ]; then
  _i_say "已布置"
  case "$(interposer_hal_state)" in
   active:*) return 0 ;;
   stock:*) return 2 ;;
   *) return 2 ;;
  esac
 fi

 # 要新建 bind：先确认没有进程映射原厂库（有就等它退出，绝不覆盖活动库）
 if [ "$dso" = 0 ] && grep -q libanc_supermoon /proc/[0-9]*/maps 2>/dev/null; then
  _i_say "有进程正在映射原厂库，本次不覆盖"
  return 3
 fi

 mkdir -p "$LIBDIR" || return 1
 mountpoint -q "$LIBDIR" || mount -t tmpfs -o size=16m tmpfs "$LIBDIR" || return 1
 cp "$DSO" "$LIBDIR/dso.so" && chmod 644 "$LIBDIR/dso.so" &&
  chcon u:object_r:vendor_file:s0 "$LIBDIR/dso.so" || return 1

 # 只有 hal=0 才会走到这里：$HAL 是原厂镜像，patch 是幂等的
 "$PATCH" "$HAL" "$LIBDIR/hal-patched" "$DSO_TARGET" || return 1
 chmod 755 "$LIBDIR/hal-patched"
 chcon u:object_r:hal_usb_dp_relay_default_exec:s0 "$LIBDIR/hal-patched" || return 1

 if [ "$dso" = 0 ]; then
  mount --bind "$LIBDIR/dso.so" "$DSO_TARGET" || return 1
 fi
 if [ "$hal" = 0 ]; then
  if ! mount --bind "$LIBDIR/hal-patched" "$HAL"; then
   [ "$dso" = 0 ] && umount "$DSO_TARGET" 2>/dev/null
   return 1
  fi
 fi
 _i_say "已布置"
 case "$(interposer_hal_state)" in
  active:*) return 0 ;;
  *) return 2 ;;
 esac
}

interposer_report() {
 echo "SVID: $([ -n "$(awk -v t="$TARGET" '$5==t {print $1}' /proc/self/mountinfo 2>/dev/null)" ] && cat "$TARGET" 2>/dev/null || echo 未挂载)"
 echo "HAL bind: $(_i_mounted "$HAL" && echo yes || echo no)"
 echo "DSO bind: $(_i_mounted "$DSO_TARGET" && echo yes || echo no)"
 echo "HAL: $(interposer_hal_state)"
}
