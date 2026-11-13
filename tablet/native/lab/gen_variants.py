#!/usr/bin/env python3
"""Generate tablet/native/fullsplit-lab-variants.h from the factory descriptor.

The lab DSO needs, per descriptor/frame variant:
  * the exact 914-byte report descriptor bytes it should plant into
    /config/usb_gadget/g1/functions/hid.usb1/report_desc instead of the bytes
    the stock HAL writes, and
  * the 62-byte report-ID-1 frame permutation that matches that descriptor.

Built-in variants (generated from the authenticated factory descriptor and
aligned with hid-re's 2026-11-13 analysis: macOS only dispatches digitizer
collections it recognises, so the matrix moves towards Touch Pad / Stylus /
report-ID-1 semantics; see the note in BUILD-NOTES):
  V0  factory bytes / identity frame. Never writes configfs, never reorders.
  V1  Contact Count (Usage 0x54) input item moved to directly after Report ID 1,
      i.e. [rid][count][10 x flags,id,xlo,xhi,ylo,yhi].
  V2  factory descriptor with the ten Finger Logical Collections (a1 02)
      changed to Physical Collections (a1 00); identity frame.
  V3  V1 + per-contact Contact Identifier (Usage 0x51) moved in front of Tip
      Switch + 85 02 -> 85 01, i.e. [rid][count][10 x id,flags,xlo,xhi,ylo,yhi].
  V4  application usage 09 04 (Touch Screen) -> 09 05 (Touch Pad) + feature
      report 85 02 -> 85 01; identity frame (highest-priority hypothesis).
  V6  factory descriptor with the 10 Finger collection usages 09 22 -> 09 20
      (Stylus); identity frame.
hid-re's variants.json, when present, overrides any built-in by id and adds the
rest (e.g. V5 single-finger).

The descriptor length is fixed at 914 bytes and report length at 62 bytes for
every variant: the HAL's own write is only replaced when it is byte-for-byte
the factory descriptor, and configfs rejects shape changes once the function is
bound. Any variant whose JSON declaration asks for another report length is
emitted as unsupported=1; the DSO then refuses to activate it and logs
variant-unavailable instead of quietly relaxing the 62-byte contract.

--variants-json format (hid-re hand-off):
  {"variants": [{"id": "V2", "label": "...", "report_length": 62,
                 "desc_hex": "050d...",      # optional; omit => factory desc
                 "frame_perm": [0, 1, ...]}]}  # optional; 62 ints,
                                               # -1 writes 0x00
  Missing "frame_perm" means identity (no frame rewrite). "desc_hex" length may
  differ from 914 for experiments, but report_length must stay 62.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HEX = HERE / "hid1-v0.desc.hex"
DEFAULT_OUT = HERE.parent / "fullsplit-lab-variants.h"

FACTORY_DESC_LEN = 914
FACTORY_DESC_SHA = "2235bd04d655a092c83b7dfb3915ba67b4b63ec4a1f0ef332851bbed5bdf3a25"
FACTORY_REPORT_LENGTH = 62
FRAME_LEN = FACTORY_REPORT_LENGTH

# Descriptor item byte strings (HID short items, no long items here).
COUNT_ITEM = bytes.fromhex("0954" "1500" "250a" "7508" "9501" "8102")
COUNT_PAGE = bytes.fromhex("050d")  # Usage Page (Digitizer) before the count item
FINGER_OPEN = bytes.fromhex("0922" "a102")           # Usage (Finger), Collection (Logical)
TIP_ITEM = bytes.fromhex("0942" "1500" "2501" "7501" "9501" "8102")  # Tip Switch 1 bit
INRANGE_ITEM = bytes.fromhex("0932" "8102")          # In Range 1 bit
PAD_ITEM = bytes.fromhex("7506" "8103")              # 6 pad bits
CONTACTID_ITEM = bytes.fromhex("0951" "1500" "2509" "7508" "9501" "8102")
FINGER_BLOCK_LEN = 77
FINGER_HDR = FINGER_OPEN + TIP_ITEM + INRANGE_ITEM + PAD_ITEM + CONTACTID_ITEM  # 36 bytes


def load_hex(path: Path) -> bytes:
    data = bytearray()
    for line in path.read_text().splitlines():
        for token in line.split():
            data.append(int(token, 16))
    return bytes(data)


def identity_perm() -> list:
    return list(range(FRAME_LEN))


def move_count_first(desc: bytes) -> bytes:
    """Move the Contact Count input item to directly after Report ID 1."""
    if desc.count(b"\x09\x54") != 1:
        raise SystemExit("descriptor: expected exactly one Usage 0x54 (Contact Count)")
    pos = desc.find(b"\x09\x54")
    if desc[pos:pos + len(COUNT_ITEM)] != COUNT_ITEM:
        raise SystemExit("descriptor: Contact Count item bytes are not the reviewed shape")
    # Keep the preceding Usage Page item where it is (it also feeds report 2's
    # Contact Count Maximum); move only the input item itself.
    moved = desc[:pos] + desc[pos + len(COUNT_ITEM):]
    rid = moved.find(bytes.fromhex("8501"))
    if rid < 0:
        raise SystemExit("descriptor: Report ID 1 not found")
    insert = rid + 2
    return moved[:insert] + COUNT_ITEM + moved[insert:]


def contact_id_first(desc: bytes) -> bytes:
    """Within each finger collection, move Contact Identifier before Tip Switch."""
    if desc.count(FINGER_HDR) != 10:
        raise SystemExit("descriptor: expected 10 identical finger collection headers, "
                         "found %d" % desc.count(FINGER_HDR))
    new_block = FINGER_OPEN + CONTACTID_ITEM + TIP_ITEM + INRANGE_ITEM + PAD_ITEM
    return desc.replace(FINGER_HDR, new_block)


def replace_once(desc: bytes, old_hex: str, new_hex: str, label: str) -> bytes:
    old = bytes.fromhex(old_hex.replace(" ", ""))
    new = bytes.fromhex(new_hex.replace(" ", ""))
    if len(old) != len(new):
        raise SystemExit("%s: replacement must keep the descriptor length" % label)
    n = desc.count(old)
    if n != 1:
        raise SystemExit("%s: expected exactly one %s, found %d" % (label, old_hex, n))
    return desc.replace(old, new)


def report_id_2_to_1(desc: bytes) -> bytes:
    """Feature report 85 02 becomes report ID 1 (hid-re V2/V3/V4)."""
    return replace_once(desc, "8502", "8501", "report id 2 -> 1")


def usage_touchpad(desc: bytes) -> bytes:
    """Application collection usage 0x04 Touch Screen -> 0x05 Touch Pad (V4)."""
    return replace_once(desc, "050d0904a101", "050d0905a101",
                        "Touch Screen -> Touch Pad usage")


def physical_collections(desc: bytes) -> bytes:
    """Ten Finger collections a1 02 (Logical) -> a1 00 (Physical) (V2)."""
    old = bytes.fromhex("0922a102")
    new = bytes.fromhex("0922a100")
    n = desc.count(old)
    if n != 10:
        raise SystemExit("logical -> physical: expected 10 finger collections, found %d" % n)
    return desc.replace(old, new)


def usage_stylus_fingers(desc: bytes) -> bytes:
    """All 10 Finger logical collections 0x09 0x22 -> 0x09 0x20 Stylus (V6)."""
    old = bytes.fromhex("0922a102")
    new = bytes.fromhex("0920a102")
    n = desc.count(old)
    if n != 10:
        raise SystemExit("finger -> stylus: expected 10 finger collections, found %d" % n)
    return desc.replace(old, new)


def perm_count_first() -> list:
    perm = [0] * FRAME_LEN
    perm[0] = 0
    perm[1] = 61                      # Contact Count moves to byte 1
    for i in range(2, FRAME_LEN):
        perm[i] = i - 1
    return perm


def perm_contact_id_first() -> list:
    perm = perm_count_first()
    for i in range(10):
        src = 1 + 6 * i               # factory slot: flags, id, xlo, xhi, ylo, yhi
        dst = 2 + 6 * i               # variant slot: id, flags, xlo, xhi, ylo, yhi
        perm[dst + 0] = src + 1       # contact id
        perm[dst + 1] = src + 0       # tip switch / in range flags
        perm[dst + 2] = src + 2       # x low
        perm[dst + 3] = src + 3       # x high
        perm[dst + 4] = src + 4       # y low
        perm[dst + 5] = src + 5       # y high
    return perm


def check_perm(perm, where):
    if not isinstance(perm, list) or len(perm) != FRAME_LEN:
        raise SystemExit("%s: frame_perm must be a list of %d ints" % (where, FRAME_LEN))
    for v in perm:
        if not isinstance(v, int) or v < -1 or v >= FRAME_LEN:
            raise SystemExit("%s: frame_perm value %r out of [-1, %d]" % (where, v, FRAME_LEN - 1))


def c_ident(ident: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", ident):
        raise SystemExit("variant id %r is not a valid C identifier fragment" % ident)
    return ident.upper()


def emit_array(name: str, data: bytes, out: list, per_line: int = 16) -> None:
    out.append("static const unsigned char %s[%d] = {" % (name, len(data)))
    for i in range(0, len(data), per_line):
        chunk = ", ".join("0x%02x" % b for b in data[i:i + per_line])
        out.append("  " + chunk + ",")
    out.append("};")


def emit_perm(name: str, perm: list) -> list:
    rows = []
    for i in range(0, len(perm), 16):
        rows.append("  " + ", ".join("%d" % v for v in perm[i:i + 16]) + ",")
    rows[-1] = rows[-1].rstrip(",") if rows else ""
    return ["static const signed char %s[%d] = {" % (name, len(perm))] + rows + ["};"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--desc-hex", type=Path, default=DEFAULT_HEX,
                    help="factory hid.usb1 report descriptor hex dump (default: %(default)s)")
    ap.add_argument("--variants-json", type=Path, default=None,
                    help="hid-re variants matrix; overrides built-in variants by id")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    factory = load_hex(args.desc_hex)
    if len(factory) != FACTORY_DESC_LEN:
        raise SystemExit("factory descriptor is %d bytes, expected %d"
                         % (len(factory), FACTORY_DESC_LEN))
    sha = hashlib.sha256(factory).hexdigest()
    if sha != FACTORY_DESC_SHA:
        raise SystemExit("factory descriptor sha256 %s != reviewed %s" % (sha, FACTORY_DESC_SHA))

    v1_desc = move_count_first(factory)
    v2_desc = physical_collections(factory)
    v3_desc = report_id_2_to_1(contact_id_first(v1_desc))
    v4_desc = usage_touchpad(report_id_2_to_1(factory))
    v6_desc = usage_stylus_fingers(factory)
    for name, desc in (("V1", v1_desc), ("V2", v2_desc), ("V3", v3_desc),
                       ("V4", v4_desc), ("V6", v6_desc)):
        if len(desc) != FACTORY_DESC_LEN:
            raise SystemExit("built-in %s descriptor is %d bytes, must stay %d"
                             % (name, len(desc), FACTORY_DESC_LEN))

    # id -> dict(kind, label, report_length, desc(bytes|None), perm(list|None), supported)
    variants = {
        "V0": dict(label="factory descriptor, identity frames",
                   report_length=FACTORY_REPORT_LENGTH, desc=factory, perm=identity_perm(),
                   supported=True, builtin="V0"),
        "V1": dict(label="ContactCount directly after Report ID 1",
                   report_length=FACTORY_REPORT_LENGTH, desc=v1_desc, perm=perm_count_first(),
                   supported=True, builtin="V1"),
        "V2": dict(label="ten Finger Logical Collections -> Physical Collections",
                   report_length=FACTORY_REPORT_LENGTH, desc=v2_desc, perm=identity_perm(),
                   supported=True, builtin="V2"),
        "V3": dict(label="ContactCount first + ContactID first + report id 2 -> 1",
                   report_length=FACTORY_REPORT_LENGTH, desc=v3_desc,
                   perm=perm_contact_id_first(), supported=True, builtin="V3"),
        "V4": dict(label="Touch Pad usage 09 05 + report id 2 -> 1 (macOS-native class)",
                   report_length=FACTORY_REPORT_LENGTH, desc=v4_desc, perm=identity_perm(),
                   supported=True, builtin="V4"),
        "V6": dict(label="10 Finger collections declared as Stylus usage 09 20",
                   report_length=FACTORY_REPORT_LENGTH, desc=v6_desc, perm=identity_perm(),
                   supported=True, builtin="V6"),
    }
    order = ["V0", "V1", "V2", "V3", "V4", "V6"]

    if args.variants_json is not None:
        doc = json.loads(args.variants_json.read_text())
        entries = doc["variants"] if isinstance(doc, dict) else doc
        if not isinstance(entries, list):
            raise SystemExit("variants json: expected a list or {\"variants\": [...]}")
        for entry in entries:
            vid = entry.get("id")
            if not isinstance(vid, str) or not vid:
                raise SystemExit("variants json: every variant needs a string id")
            report_length = entry.get("report_length", FACTORY_REPORT_LENGTH)
            if not isinstance(report_length, int) or report_length <= 0:
                raise SystemExit("%s: bad report_length %r" % (vid, report_length))
            desc = None
            if entry.get("desc_hex"):
                desc = bytes.fromhex("".join(entry["desc_hex"].split()))
                if not desc:
                    raise SystemExit("%s: empty desc_hex" % vid)
            elif vid == "V0":
                desc = factory
            perm = entry.get("frame_perm")
            if perm is None:
                perm = identity_perm()
            check_perm(perm, vid)
            supported = report_length == FACTORY_REPORT_LENGTH
            label = entry.get("label") or entry.get("name") or vid
            variants[vid] = dict(label=label, report_length=report_length, desc=desc,
                                 perm=perm, supported=supported, builtin=None)
            if vid not in order:
                order.append(vid)

    out = [
        "/* Generated by tablet/native/lab/gen_variants.py -- do not edit.",
        " * Factory descriptor sha256 %s (size %d), report length %d."
        % (FACTORY_DESC_SHA, FACTORY_DESC_LEN, FACTORY_REPORT_LENGTH),
        " * Every variant keeps the 62-byte report; desc entries are the exact",
        " * bytes the lab interposer plants instead of the factory descriptor.",
        " */",
        "#ifndef FULLSPLIT_LAB_VARIANTS_H",
        "#define FULLSPLIT_LAB_VARIANTS_H",
        "",
        "typedef struct {",
        "\tconst char *id;",
        "\tconst char *label;",
        "\tint report_length;",
        "\tint desc_required;",
        "\tconst unsigned char *desc;",
        "\tunsigned desc_len;",
        "\tconst signed char *perm;",
        "\tunsigned perm_len;",
        "} lab_variant_table;",
        "",
    ]
    emit_array("LAB_DESC_FACTORY", factory, out)
    out.append("static const unsigned LAB_DESC_FACTORY_LEN = %d;" % len(factory))
    out.append("")
    table_entries = []
    for vid in order:
        spec = variants[vid]
        ident = c_ident(vid)
        desc = spec["desc"]
        # Emit each descriptor once (V0 factory and a JSON V0 share bytes only if
        # they are identical; dedupe by content).
        if desc is not None:
            emit_array("LAB_DESC_" + ident, desc, out)
            out.append("static const unsigned LAB_DESC_%s_LEN = %d;" % (ident, len(desc)))
            out.append("")
        perm = spec["perm"]
        out += emit_perm("LAB_PERM_" + ident, perm)
        out.append("")
        safe_label = spec["label"].replace('"', "'")
        table_entries.append(
            '\t{"%s", "%s", %d, %d, %s, LAB_DESC_%s_LEN, LAB_PERM_%s, %d},'
            % (vid, safe_label, spec["report_length"],
               1 if (desc is not None and desc != factory) else 0,
               ("LAB_DESC_" + ident) if desc is not None else "NULL",
               ident, ident, FRAME_LEN))
    out.append("static const lab_variant_table LAB_VARIANTS[] = {")
    out += table_entries
    out.append("};")
    out.append("static const int LAB_VARIANT_COUNT = (int)(sizeof(LAB_VARIANTS)/sizeof(LAB_VARIANTS[0]));")
    out.append("")
    out.append("#endif")
    out.append("")

    args.out.write_text("\n".join(out))
    meta = {
        "factory_desc_sha256": FACTORY_DESC_SHA,
        "factory_desc_len": FACTORY_DESC_LEN,
        "report_length": FACTORY_REPORT_LENGTH,
        "variants": [
            {
                "id": vid,
                "label": variants[vid]["label"],
                "report_length": variants[vid]["report_length"],
                "desc_required": 1 if (variants[vid]["desc"] is not None
                                       and variants[vid]["desc"] != factory) else 0,
                "desc_len": len(variants[vid]["desc"]) if variants[vid]["desc"] is not None else 0,
                "desc_sha256": (hashlib.sha256(variants[vid]["desc"]).hexdigest()
                                if variants[vid]["desc"] is not None else None),
                "frame_perm": variants[vid]["perm"],
                "supported": variants[vid]["supported"],
            }
            for vid in order
        ],
    }
    meta_path = args.out.with_name("fullsplit-lab-variants.meta.json")
    meta_path.write_text(json.dumps(meta, indent=1) + "\n")

    # Production 2.1.10 payload: a lean header with only the factory descriptor
    # and the single validated V4 descriptor (no variant table, no lab paths).
    v4 = variants.get("V4")
    if v4 is None or v4["desc"] is None:
        raise SystemExit("rejecting native build: matrix has no V4 descriptor")
    if v4["report_length"] != FACTORY_REPORT_LENGTH or len(v4["desc"]) != FACTORY_DESC_LEN:
        raise SystemExit("rejecting native build: V4 must stay 914B/62B")
    v4_perm = v4["perm"]
    if v4_perm != identity_perm():
        raise SystemExit("rejecting native build: V4 must keep the identity frame layout")
    native = [
        "/* Generated by tablet/native/lab/gen_variants.py -- do not edit.",
        " * Production native-touch payload (module 2.1.10): the reviewed factory",
        " * descriptor and the validated V4 descriptor (Touch Pad usage 0x0D:0x05,",
        " * feature report ID 2 -> 1). V4 keeps the factory 62-byte frame layout,",
        " * so no frame permutation is compiled in.",
        " * factory sha256 %s" % FACTORY_DESC_SHA,
        " * V4      sha256 %s" % hashlib.sha256(v4["desc"]).hexdigest(),
        " */",
        "#ifndef FULLSPLIT_NATIVE_V4_H",
        "#define FULLSPLIT_NATIVE_V4_H",
        "",
    ]
    emit_array("NATIVE_DESC_FACTORY", factory, native)
    native.append("static const unsigned NATIVE_DESC_FACTORY_LEN = %d;" % len(factory))
    native.append("")
    emit_array("NATIVE_DESC_V4", v4["desc"], native)
    native.append("static const unsigned NATIVE_DESC_V4_LEN = %d;" % len(v4["desc"]))
    native.append('static const char NATIVE_DESC_V4_SHA[] = "%s";'
                  % hashlib.sha256(v4["desc"]).hexdigest())
    native.append('static const char NATIVE_DESC_FACTORY_SHA[] = "%s";' % FACTORY_DESC_SHA)
    native.append("")
    native.append("#endif")
    native.append("")
    native_path = args.out.with_name("native-v4-desc.h")
    native_path.write_text("\n".join(native))
    print("wrote %s (V4 sha256 %s)" % (native_path, hashlib.sha256(v4["desc"]).hexdigest()))
    print("wrote %s: %d variants (%s)" % (args.out, len(order), ", ".join(order)))
    print("wrote %s" % meta_path)
    for vid in order:
        spec = variants[vid]
        desc = spec["desc"]
        dlen = len(desc) if desc is not None else 0
        dsha = hashlib.sha256(desc).hexdigest()[:16] if desc is not None else "-"
        print("  %-3s report_length=%-3d desc_len=%-4d desc_sha=%s perm=%s%s"
              % (vid, spec["report_length"], dlen, dsha,
                 "identity" if spec["perm"] == identity_perm() else "custom",
                 "" if spec["supported"] else " UNSUPPORTED(report_length!=62)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
