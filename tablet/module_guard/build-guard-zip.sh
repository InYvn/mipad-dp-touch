#!/bin/sh
# build-guard-zip.sh — 把 module/ 打成 KernelSU 可刷入的 zip（确定性：固定时间戳 + 排序）。
# 不装到设备；只是生成 out/mipad_dpin_guard-<version>.zip 并校验内容/权限。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
MOD="$HERE/module"
OUT="$HERE/out"
VER=$(awk -F= '$1=="version"{print $2; exit}' "$MOD/module.prop")
NAME="mipad_dpin_guard-$VER.zip"
mkdir -p "$OUT"

# 覆盖前留档（不删除）
if [ -f "$OUT/$NAME" ]; then
	mkdir -p "$OUT/archive"
	mv "$OUT/$NAME" "$OUT/archive/$(date +%Y%m%d-%H%M%S)-$NAME"
	echo "旧包已归档: $OUT/archive/*-$NAME"
fi

# 语法检查（POSIX：用 dash 时更严格）
for f in "$MOD/post-fs-data.sh" "$MOD/service.sh" "$MOD/uninstall.sh" "$MOD"/bin/*.sh; do
	if command -v dash >/dev/null 2>&1; then
		dash -n "$f" || {
			echo "syntax FAIL: $f" >&2
			exit 1
		}
	else
		sh -n "$f" || {
			echo "syntax FAIL: $f" >&2
			exit 1
		}
	fi
done

python3 - "$MOD" "$OUT/$NAME" <<'PY'
import os
import sys
import zipfile

mod, out = sys.argv[1], sys.argv[2]
items = []
for root, dirs, names in os.walk(mod):
    dirs.sort()
    names.sort()
    for n in names:
        p = os.path.join(root, n)
        rel = os.path.relpath(p, mod).replace(os.sep, "/")
        items.append((rel, p))

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for rel, p in sorted(items, key=lambda x: x[0]):
        zi = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        mode = 0o755 if rel.endswith(".sh") else 0o644
        if rel == "module.prop":
            mode = 0o644
        zi.external_attr = (0o100000 | mode) << 16
        with open(p, "rb") as fh:
            z.writestr(zi, fh.read())
print("wrote", out)
PY

# 校验：条目齐全、脚本可执行位、解包后 selftest 可跑
LIST=$(unzip -l "$OUT/$NAME" | awk '{print $4}' | grep -v '^$' | grep -v 'Name' || true)
for need in module.prop service.sh post-fs-data.sh uninstall.sh bin/dpin-lib.sh bin/dpin-guard.sh bin/dpin-recover.sh; do
	if ! printf '%s\n' "$LIST" | grep -qx "$need"; then
		echo "MISSING in zip: $need" >&2
		exit 1
	fi
done
if ! unzip -Z -l "$OUT/$NAME" | grep -E '^-rwxr-xr-x .*bin/dpin-guard\.sh' >/dev/null; then
	echo "WARN: bin/dpin-guard.sh 在 zip 里可能没有可执行位（KernelSU 用 sh 调用，不影响；记录以便核查）" >&2
fi

VERIFY="$OUT/verify-$VER"
mkdir -p "$VERIFY"
unzip -o -q "$OUT/$NAME" -d "$VERIFY"
if command -v dash >/dev/null 2>&1; then
	dash "$VERIFY/bin/dpin-guard.sh" selftest >"$VERIFY/selftest.log" 2>&1 || {
		echo "selftest FAIL（解包后），见 $VERIFY/selftest.log" >&2
		exit 1
	}
	tail -n 5 "$VERIFY/selftest.log"
fi

SHA=$(shasum -a 256 "$OUT/$NAME" | awk '{print $1}')
SIZE=$(wc -c <"$OUT/$NAME" | tr -d ' ')
printf 'ZIP=%s\nSHA256=%s\nSIZE=%s\n' "$OUT/$NAME" "$SHA" "$SIZE"
