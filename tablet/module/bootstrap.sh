#!/system/bin/sh
# Invoked once under the native controller flock; never touches HAL or sysfs.
set -eu
[ "$1" = stage ] || exit 64
S=$2
M=$3
if [ ! -f "$S/runtime-ready" ]; then
 (cd "$M" && sha256sum -c installed.sha256) || exit 70
 for f in module-controller platform-tool prepare-owner fullsplit-bounded-v2.so platform.sh profile.sh common.sh runtime-monitor.sh; do
  [ ! -L "$S/$f" ] || exit 70
  cp "$M/$f" "$S/$f.new"; chmod 700 "$S/$f.new"; mv "$S/$f.new" "$S/$f"
 done
 chmod 600 "$S/fullsplit-bounded-v2.so"
 cp "$M/runtime.sha256" "$S/runtime.sha256"
 (cd "$S" && sha256sum -c runtime.sha256) || exit 70
 touch "$S/runtime-ready"; sync
fi
(cd "$S" && sha256sum -c runtime.sha256) || exit 70
