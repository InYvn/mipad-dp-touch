#!/system/bin/sh
# Persistent backing and recovery record; never adopt pre-existing/foreign mounts.
STATE=/data/adb/mipad_dptouch_v102
TARGET=/sys/devices/platform/mca_strategy_pd_auth/adapter_svid
FAKE=$STATE/fake_svid
OWNER=$STATE/owner
LOCK=$STATE/lock
fail() { echo "Mipad DP Touch: $*" >&2; return 1; }
ns() { readlink /proc/self/ns/mnt; }
boot() { cat /proc/sys/kernel/random/boot_id; }
entry() { awk -v t="$TARGET" '$5==t {print $1}' /proc/self/mountinfo; }
identity() { stat -c '%d:%i:%u:%g:%a' "$1"; }
# Require global init namespace; never fall back to an invisible private mount.
global_check() {
 [ "$(ns)" = "$(readlink /proc/1/ns/mnt)" ] || return 1
 for p in $(pidof vendor.xiaomi.usb.dp-service); do
  [ "$(readlink /proc/$p/ns/mnt)" = "$(ns)" ] || return 1
 done
}
owned() {
 [ -f "$OWNER" ] && [ ! -L "$OWNER" ] || return 1
 [ "$(wc -l < "$OWNER" | tr -d ' ')" = 5 ] || return 1
 { IFS= read -r ob; IFS= read -r on; IFS= read -r oi; IFS= read -r of; IFS= read -r ot; } < "$OWNER"
 [ "$ob" = "$(boot)" ] && [ "$on" = "$(ns)" ] &&
 [ -n "$oi" ] && [ "$oi" = "$(entry)" ] &&
 [ "$of" = "$(identity "$FAKE")" ] &&
 [ "$ot" = "$(identity "$TARGET")" ] && [ "$of" = "$ot" ] &&
 [ "$(cat "$FAKE")" = 0 ] && [ "$(cat "$TARGET")" = 0 ]
}
apply() {
 # 版本契约只用于决定是否注入 DSO；SVID 是通用的一层，未审计固件也照挂，
 # 否则一次 OTA 就会让整个模块静默失效。这里只保留结构性的安全检查。
 [ ! -f "$STATE/off" ] || return 0
 if [ -n "$(entry)" ]; then owned || { fail 'foreign or unrecorded mount: not adopted'; return 1; }; return 0; fi
 [ -e "$TARGET" ] && [ ! -L "$TARGET" ] || return 1
 # Original sysfs SVID is hexadecimal, observed bytes 30 35 61 63 (05ac).
 original=$(cat "$TARGET") || return 1
 case "$original" in ''|*[!0123456789aAbBcCdDeEfF]*) fail 'invalid original SVID'; return 1;; esac
 [ "${#original}" -le 4 ] || return 1
 [ "$(stat -f -c %T "$TARGET")" = sysfs ] || return 1
 [ "$(stat -c '%u:%g:%a' "$TARGET")" = 0:0:440 ] || return 1
 # Reboot-invalid records are safe to discard ONLY when no target mount exists.
 rm -f "$OWNER" || return 1
 [ ! -L "$FAKE" ] || return 1
 printf 0 > "$FAKE" && chmod 644 "$FAKE" || return 1
 # Bind mounts retain the backing inode label. HAL reads this file fine without
 # the vendor label on this firmware (verified); relabel is best-effort because
 # the ksu domain is not granted relabelto for it.
 chcon u:object_r:vendor_sysfs_xm_power:s0 "$FAKE" 2>/dev/null || true
 # Write-ahead intent binds recovery to this boot, namespace and exact inode.
 { boot; ns; identity "$FAKE"; } > "$STATE/mount-intent.new" &&
 mv "$STATE/mount-intent.new" "$STATE/mount-intent" && sync || return 1
 mount --bind "$FAKE" "$TARGET" || return 1
 mid=$(entry)
 [ -n "$mid" ] && [ "$(identity "$FAKE")" = "$(identity "$TARGET")" ] || {
  fail 'post-mount verification failed; retain state; no blind rollback'; return 1;
 }
 { boot; ns; printf '%s\n' "$mid"; identity "$FAKE"; identity "$TARGET"; } > "$OWNER.new" &&
 mv "$OWNER.new" "$OWNER" || return 1
 owned || return 1
 rm -f "$STATE/mount-intent"
}
recover_mount_intent() {
 [ ! -e "$OWNER" ] && [ ! -L "$OWNER" ] || return 1
 [ -f "$STATE/mount-intent" ] && [ ! -L "$STATE/mount-intent" ] && [ ! -L "$FAKE" ] || return 1
 [ "$(wc -l < "$STATE/mount-intent" | tr -d ' ')" = 3 ] || return 1
 { IFS= read -r ib; IFS= read -r ins; IFS= read -r iid; } < "$STATE/mount-intent"
 [ "$ib" = "$(boot)" ] && [ "$ins" = "$(ns)" ] && [ "$iid" = "$(identity "$FAKE")" ] &&
 [ "$iid" = "$(identity "$TARGET")" ] && [ "$(cat "$FAKE")" = 0 ] && [ "$(cat "$TARGET")" = 0 ] || return 1
 mid=$(entry); [ -n "$mid" ] || return 1
 { boot; ns; printf '%s\n' "$mid"; identity "$FAKE"; identity "$TARGET"; } > "$OWNER.new" && mv "$OWNER.new" "$OWNER" || return 1
 owned
}
remove_owned() {
 if [ -z "$(entry)" ]; then rm -f "$OWNER" "$STATE/mount-intent"; return $?; fi
 if [ ! -e "$OWNER" ]; then recover_mount_intent || { fail 'refuse foreign mount without exact write-ahead intent'; return 1; }; fi
 owned || { fail 'refuse to unmount foreign/changed/unrecorded target'; return 1; }
 umount "$TARGET" || { fail 'unmount failed: retain owner and backing'; return 1; }
 [ -z "$(entry)" ] || { fail 'mount remains: retain recovery state'; return 1; }
 rm -f "$OWNER" "$STATE/mount-intent"
}
run_locked() (
 global_check || { fail 'namespace mismatch; no fallback'; return 1; }
 [ ! -L "$STATE" ] || return 1
 mkdir -p "$STATE" && chmod 700 "$STATE" || return 1
 # mkdir lock is atomic. Stale lock never stolen: delete after reboot only.
 if ! mkdir "$LOCK" 2>/dev/null; then
  [ -f "$LOCK/boot" ] && [ "$(cat "$LOCK/boot")" != "$(boot)" ] || { fail 'busy or stale same-boot lock'; return 1; }
  rm -f "$LOCK/boot" && rmdir "$LOCK" && mkdir "$LOCK" || return 1
 fi
 boot > "$LOCK/boot" || return 1
 trap 'rm -f "$LOCK/boot"; rmdir "$LOCK"' EXIT
 trap 'exit 1' HUP INT TERM
 case "$1" in
 apply) apply;;
 disable|uninstall) touch "$STATE/off" && remove_owned;;
 enable) rm -f "$STATE/off" && apply;;
 status) if owned; then echo owned; else echo inactive-or-foreign; return 1; fi;;
 toggle) if [ -f "$STATE/off" ]; then rm -f "$STATE/off" && apply; else touch "$STATE/off" && remove_owned; fi;;
 *) return 1;;
 esac
)
