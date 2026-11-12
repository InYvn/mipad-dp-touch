#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成插件市场用的**真实预览图**，写进 src/plugin_icons.py（base64 PNG）。

源图放在 assets/plugin-previews/：
    touchkeys.png  键盘的实际渲染图
    touchbar.png   工具条的实际渲染图

键盘用「完整放进画布」(fit)；工具条太扁，用「填满后裁中间」(cover)，
否则缩到卡片宽度只剩一条线。输出 192×128（卡片里按 96×64 显示）。
"""
import base64
import os

from AppKit import (
    NSBitmapImageFileTypePNG, NSBitmapImageRep, NSBezierPath, NSColor,
    NSDeviceRGBColorSpace, NSGraphicsContext, NSImage, NSMakeRect,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "assets", "plugin-previews")
OUT = os.path.join(ROOT, "src", "plugin_icons.py")

W, H = 192, 128
PAD = 8

SPECS = {
    "touchkeys": {"file": "touchkeys.png", "mode": "fit", "pad": 4,
                  "bg": (0.95, 0.95, 0.96, 1.0)},
    # 三条不同 App 的工具条，展示「跟着前台 App 变」
    "touchbar": {"files": ["touchbar-finder.png", "touchbar-calendar.png", "touchbar-word.png"],
                 "mode": "stack", "bg": (0.10, 0.10, 0.12, 1.0)},
}


def render(spec):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, W, H, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0, 0, W, H), 14, 14)
    r, g, b, a = spec["bg"]
    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).set()
    bg.fill()
    bg.addClip()

    if spec["mode"] == "stack":
        files = spec["files"]
        band = H / len(files)
        for i, name in enumerate(files):
            path = os.path.join(SRC, name)
            img = NSImage.alloc().initWithContentsOfFile_(path)
            if img is None:
                raise SystemExit("missing " + path)
            iw, ih = img.size().width, img.size().height
            scale = (W - 2 * PAD) / iw
            dw, dh = iw * scale, ih * scale
            dx = (W - dw) / 2
            dy = H - (i + 1) * band + (band - dh) / 2
            img.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(dx, dy, dw, dh), NSMakeRect(0, 0, 0, 0), 2, 1.0)
    else:
        path = os.path.join(SRC, spec["file"])
        img = NSImage.alloc().initWithContentsOfFile_(path)
        if img is None:
            raise SystemExit("missing " + path)
        iw, ih = img.size().width, img.size().height
        pad = spec.get("pad", PAD)
        if spec["mode"] == "fit":
            scale = min((W - 2 * pad) / iw, (H - 2 * pad) / ih)
        else:
            scale = max(W / iw, H / ih)
        dw, dh = iw * scale, ih * scale
        dx, dy = (W - dw) / 2, (H - dh) / 2
        img.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(dx, dy, dw, dh), NSMakeRect(0, 0, 0, 0), 2, 1.0)

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return base64.b64encode(bytes(data)).decode("ascii")


def main():
    icons = {k: render(v) for k, v in SPECS.items()}
    with open(OUT, "w", encoding="utf-8") as f:
        f.write('#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n')
        f.write('"""插件市场预览图（192×128 PNG, base64）。由 tools/make_plugin_icons.py 生成，勿手改。"""\n\n')
        f.write("ICONS = {\n")
        for k, v in icons.items():
            f.write('    "%s": (\n' % k)
            for i in range(0, len(v), 76):
                f.write('        "%s"\n' % v[i:i + 76])
            f.write("    ),\n")
        f.write("}\n")
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
