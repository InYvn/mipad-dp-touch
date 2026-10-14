#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 10.3GB OTA 全量包只取 boot.img / init_boot.img（HTTP Range + payload manifest）。
data_offset 经验基准 = payload 起点 + 24 + manifest_size + signatures_size（已用 XZ 魔数对齐 0 误差验证）。"""
import urllib.request, struct, hashlib, os, json, time, lzma, bz2

URL = ("https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/"
       "OS4.0.10.0.XBMCNXM/yingtian-ota_full-OS4.0.10.0.XBMCNXM-user-17.0-b8fd7d392d.zip")
OUT_DIR = os.path.expanduser("~/Desktop/mipad-firmware")
WANT = ["boot", "init_boot"]
TYPES = {0: "REPLACE", 1: "REPLACE_BZ", 8: "REPLACE_XZ"}

def rng(start, end):
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{end}"})
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as e:
            print("   retry", a, e); time.sleep(4)
    raise SystemExit("range fetch failed")

def rd_varint(b, i):
    r = 0; s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80): return r, i
        s += 7

def fields(b):
    i = 0
    while i < len(b):
        key, i = rd_varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rd_varint(b, i)
        elif wt == 2:
            ln, i = rd_varint(b, i); v = b[i:i + ln]; i += ln
        elif wt == 1:
            v = b[i:i + 8]; i += 8
        elif wt == 5:
            v = b[i:i + 4]; i += 4
        else:
            return
        yield fn, wt, v

# ---- 定位 payload.bin 与 manifest ----
c = json.load(open("/tmp/ota_cache.json"))
data_start, msize = c["data_start"], c["msize"]
man = open("/tmp/ota_manifest.bin", "rb").read()
sig_size = 0
for fn, wt, v in fields(man):
    if fn == 5 and wt == 0:
        sig_size = v
BLOB = data_start + 24 + msize + sig_size
print(f"[base] payload={data_start} manifest={msize} sig={sig_size} -> data blob={BLOB}")

parts = {}
for fn, wt, v in fields(man):
    if fn == 13 and wt == 2:
        pname = None; ops = []; ni = {}
        for f2, w2, v2 in fields(v):
            if f2 == 1 and w2 == 2:
                pname = v2.decode("utf-8", "replace")
            elif f2 == 8 and w2 == 2:
                op = {}
                for f3, w3, v3 in fields(v2):
                    op[f3] = v3
                ops.append(op)
            elif f2 == 7 and w2 == 2:
                for f3, w3, v3 in fields(v2):
                    ni[f3] = v3
        if pname:
            parts[pname] = (ops, ni)

os.makedirs(OUT_DIR, exist_ok=True)
for name in WANT:
    if name not in parts:
        print(f"[{name}] 分区不存在"); continue
    ops, ni = parts[name]
    exp_size = ni.get(1); exp_hash = ni.get(2, b"").hex()
    print(f"[{name}] {len(ops)} ops, 期望大小={exp_size}, 期望sha256={exp_hash[:24]}...")
    chunks, total, bad = [], 0, 0
    for k, op in enumerate(ops):
        t, doff, dlen, sha = op.get(1), op.get(2), op.get(3), op.get(8, b"").hex()
        if t not in TYPES:
            print(f"   op{k} 不支持的 type={t}"); bad += 1; break
        got = 0; raw = b""
        while got < dlen:                     # 大 op 分片拉
            n = min(4 << 20, dlen - got)
            raw += rng(BLOB + doff + got, BLOB + doff + got + n - 1)
            got += n
        if hashlib.sha256(raw).hexdigest() != sha:
            print(f"   op{k} sha 不匹配 (doff={doff} dlen={dlen})"); bad += 1; break
        if t == 1:
            raw = bz2.decompress(raw)
        elif t == 8:
            raw = lzma.decompress(raw)
        chunks.append(raw); total += len(raw)
    if bad:
        print(f"[{name}] 失败，跳过"); continue
    img = b"".join(chunks)
    h = hashlib.sha256(img).hexdigest()
    path = os.path.join(OUT_DIR, f"{name}.img")
    open(path, "wb").write(img)
    print(f"[{name}] 写出 {len(img)} bytes  sha256={h[:24]}...  "
          f"大小校验={'OK' if exp_size == len(img) else 'MISMATCH'}  哈希校验={'OK' if h == exp_hash else 'MISMATCH'}")
    print(f"         -> {path}")
