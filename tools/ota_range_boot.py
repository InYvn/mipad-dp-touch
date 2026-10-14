#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 10.3GB OTA 全量包里只取 boot.img：HTTP Range + payload manifest 解析。
下载量 ~100MB，不必下 10GB。"""
import urllib.request, struct, hashlib, io, bz2, lzma, sys, os, time

URL = ("https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/"
       "OS4.0.10.0.XBMCNXM/yingtian-ota_full-OS4.0.10.0.XBMCNXM-user-17.0-b8fd7d392d.zip")
OUT_DIR = os.path.expanduser("~/Desktop/mipad-firmware")
WANT = {"boot", "init_boot", "xbl_config"}

def rng(start, end):
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{end}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:
            print(f"   [retry {attempt}] {e}")
            time.sleep(3)
    raise SystemExit("range fetch failed")

def head_size():
    req = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])

# ---------- 1. ZIP tail -> EOCD ----------
size = head_size()
print(f"[1] zip size = {size} bytes")
tail = rng(max(0, size - 262144), size - 1)
i = tail.rfind(b"PK\x05\x06")
if i < 0:
    raise SystemExit("no EOCD")
eocd = tail[i:i + 22]
(_, _, _, _, n_ent, cd_size, cd_off) = struct.unpack("<IHHHHII", eocd[:20])
zip64 = False
if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or n_ent == 0xFFFF:
    zip64 = True
    j = tail.rfind(b"PK\x06\x07")
    rec_off = struct.unpack("<Q", tail[j + 8:j + 16])[0]
    rec = rng(rec_off, rec_off + 56 + 8 * n_ent)
    cd_size, cd_off = struct.unpack("<QQ", rec[40:56])
print(f"[1] zip64={zip64} entries={n_ent} cd={cd_off}+{cd_size}")

# ---------- 2. central directory -> payload.bin ----------
cd = rng(cd_off, cd_off + cd_size - 1)
p = 0
entries = {}
while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
    (_, _, _, _, _, _, _, _, csize, usize, nlen, elen, clen, _, _, _, off) = \
        struct.unpack("<IHHHHHHIIIHHHHHII", cd[p:p + 46])
    name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
    extra = cd[p + 46 + nlen:p + 46 + nlen + elen]
    if off == 0xFFFFFFFF or csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
        q = 0
        while q + 4 <= len(extra):
            hid, hsz = struct.unpack("<HH", extra[q:q + 4])
            if hid == 0x0001:
                vals = extra[q + 4:q + 4 + hsz]
                v = []
                k = 0
                while k + 8 <= len(vals):
                    v.append(struct.unpack("<Q", vals[k:k + 8])[0]); k += 8
                it = iter(v)
                if usize == 0xFFFFFFFF: usize = next(it)
                if csize == 0xFFFFFFFF: csize = next(it)
                if off == 0xFFFFFFFF: off = next(it)
            q += 4 + hsz
    entries[name] = {"csize": csize, "usize": usize, "off": off}
    p += 46 + nlen + elen + clen
print(f"[2] entries={list(entries)[:12]}")
pl = entries.get("payload.bin")
if not pl:
    raise SystemExit("no payload.bin")
# local header 长度
lh = rng(pl["off"], pl["off"] + 29)
nlen, elen = struct.unpack("<HH", lh[26:30])
data_start = pl["off"] + 30 + nlen + elen
print(f"[2] payload.bin data_start={data_start} csize={pl['csize']}")

# ---------- 3. payload manifest ----------
hdr = rng(data_start, data_start + 24)
assert hdr[:4] == b"CrAU", hdr[:8]
ver, msize = struct.unpack(">QQ", hdr[4:20])
man = rng(data_start + 24, data_start + 24 + msize - 1)
print(f"[3] payload v{ver} manifest {msize} bytes")

def rd_varint(b, i):
    r = 0; s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80): return r, i
        s += 7

def fields(b):
    """yield (field_no, wire_type, value) ; value=bytes for wt2, int otherwise"""
    i = 0
    while i < len(b):
        try:
            key, i = rd_varint(b, i)
        except IndexError:
            return
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rd_varint(b, i)
        elif wt == 2:
            ln, i = rd_varint(b, i)
            v = b[i:i + ln]; i += ln
        elif wt == 1:
            v = b[i:i + 8]; i += 8
        elif wt == 5:
            v = b[i:i + 4]; i += 4
        else:
            return
        yield fn, wt, v

TYPE_NAME = {0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF", 4: "SOURCE_COPY",
             5: "SOURCE_BSDIFF", 6: "ZERO", 7: "DISCARD", 8: "REPLACE_XZ", 9: "PUFFDIFF"}

result = {}
for fn, wt, v in fields(man):
    if fn == 13 and wt == 2:                       # PartitionUpdate
        pname = None; ops = []; newinfo = None
        for f2, w2, v2 in fields(v):
            if f2 == 1 and w2 == 2:
                pname = v2.decode("utf-8", "replace")
            elif f2 == 8 and w2 == 2:              # operations
                op = {}
                for f3, w3, v3 in fields(v2):
                    if f3 == 1:
                        op["type"] = v3
                    elif f3 == 2:
                        op["doff"] = v3
                    elif f3 == 3:
                        op["dlen"] = v3
                    elif f3 == 8 and w3 == 2:
                        op["sha"] = v3.hex()
                ops.append(op)
            elif f2 == 7 and w2 == 2:              # new_partition_info
                for f3, w3, v3 in fields(v2):
                    if f3 == 2 and w3 == 2:
                        newinfo = v3.hex()
        if pname:
            result[pname] = {"ops": ops, "newinfo": newinfo}
print(f"[3] partitions: {sorted(result.keys())}")

def extract(pname):
    info = result.get(pname)
    if not info:
        return None
    chunks = []
    for op in info["ops"]:
        t = op.get("type"); off = op.get("doff"); ln = op.get("dlen")
        if t not in (0, 1, 8) or off is None:
            print(f"   !! 不支持的 op type={TYPE_NAME.get(t, t)}")
            return None
        raw = rng(data_start + off, data_start + off + ln - 1)
        if hashlib.sha256(raw).hexdigest() != op.get("sha"):
            print(f"   !! op sha256 不匹配 (off={off})")
            return None
        if t == 1:
            raw = bz2.decompress(raw)
        elif t == 8:
            raw = lzma.decompress(raw)
        chunks.append(raw)
    img = b"".join(chunks)
    return img, info.get("newinfo")

os.makedirs(OUT_DIR, exist_ok=True)
for name in sorted(WANT):
    if name not in result:
        continue
    got = extract(name)
    if not got:
        print(f"[4] {name}: 跳过")
        continue
    img, expect = got
    h = hashlib.sha256(img).hexdigest()
    ok = (expect == h) if expect else None
    path = os.path.join(OUT_DIR, f"{name}.img")
    open(path, "wb").write(img)
    print(f"[4] {name}.img  {len(img)} bytes  sha256={h[:24]}...  分区hash校验={'OK' if ok else (ok if ok is not None else 'n/a')}")
print("[done]", OUT_DIR)
