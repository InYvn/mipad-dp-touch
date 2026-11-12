#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成插件市场用的插件图片，写进 src/plugin_icons.py（base64 PNG）。

市场页按 id 取图；取不到就退回 SF Symbol。这样不需要处理 App 资源路径。
"""
import base64
import io
import os

from AppKit import (
    NSBitmapImageFileTypePNG, NSBitmapImageRep, NSBezierPath, NSColor,
    NSDeviceRGBColorSpace, NSGraphicsContext, NSMakeRect,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "src", "plugin_icons.py")
SIZE = 128


def _canvas():
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    return rep


def _finish(rep):
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return base64.b64encode(bytes(data)).decode("ascii")


def _rounded(x, y, w, h, r, color):
    p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), r, r)
    color.set()
    p.fill()


def touchkeys_icon():
    rep = _canvas()
    _rounded(0, 0, SIZE, SIZE, 28, NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.42, 0.92, 1))
    # 键盘底板
    _rounded(18, 34, 92, 60, 12, NSColor.colorWithCalibratedWhite_alpha_(1, 0.16))
    # 键位 3x2 + 空格
    for row in range(3):
        for col in range(4):
            _rounded(26 + col * 20, 86 - row * 16, 14, 11, 3,
                     NSColor.colorWithCalibratedWhite_alpha_(1, 0.92))
    _rounded(46, 42, 36, 9, 3, NSColor.colorWithCalibratedWhite_alpha_(1, 0.92))
    return _finish(rep)


def touchbar_icon():
    rep = _canvas()
    _rounded(0, 0, SIZE, SIZE, 28, NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.14, 1))
    # 一条工具栏
    _rounded(14, 48, 100, 32, 10, NSColor.colorWithCalibratedWhite_alpha_(1, 0.12))
    n, pad, cell = 5, 6, 20
    step = (100 - 2 * pad - cell) / (n - 1)
    for i in range(n):
        c = (NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.52, 0.98, 1) if i == 1
             else NSColor.colorWithCalibratedWhite_alpha_(1, 0.88))
        _rounded(14 + pad + i * step, 48 + pad, cell, cell, 6, c)
    return _finish(rep)


def main():
    icons = {"touchkeys": touchkeys_icon(), "touchbar": touchbar_icon()}
    with open(OUT, "w", encoding="utf-8") as f:
        f.write('#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n')
        f.write('"""插件市场图标（128×128 PNG, base64）。由 tools/make_plugin_icons.py 生成。"""\n\n')
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
