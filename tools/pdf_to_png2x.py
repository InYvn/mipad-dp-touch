#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把设置窗口的 PDF 快照渲染成 2x PNG —— 给 README / 文档配图用。

背景: `DPTOUCH_SELFTEST_WINDOW=shot:<path>` 会同时写一份 PNG 和一份 PDF。
PNG 走的是 `cacheDisplayInRect`，抓的是**那块屏当时的 backing scale** ——
窗口要是落在非 Retina 的屏上，出来就是 1x（620 宽），在 Retina 屏上看文档会发虚。
PDF 是矢量的，这里按 2x 重渲染，得到 1240 宽的清晰图。

用法: python3 tools/pdf_to_png2x.py docs/window.png.pdf docs/window.png [倍率]
"""

import os
import sys

import Quartz


def render(pdf_path, png_path, scale=2.0):
    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, pdf_path.encode("utf-8"), len(pdf_path.encode("utf-8")), False)
    doc = Quartz.CGPDFDocumentCreateWithURL(url)
    if doc is None:
        raise SystemExit("打不开 PDF: %s" % pdf_path)
    page = Quartz.CGPDFDocumentGetPage(doc, 1)
    if page is None:
        raise SystemExit("PDF 第一页读不出来: %s" % pdf_path)
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    w, h = int(round(box.size.width * scale)), int(round(box.size.height * scale))
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst)
    # 窗口圆角外的那点像素保持透明, 所以不铺底色
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextSetShouldAntialias(ctx, True)
    Quartz.CGContextSetShouldSmoothFonts(ctx, True)
    Quartz.CGContextDrawPDFPage(ctx, page)
    img = Quartz.CGBitmapContextCreateImage(ctx)
    out = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, png_path.encode("utf-8"), len(png_path.encode("utf-8")), False)
    dest = Quartz.CGImageDestinationCreateWithURL(out, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, img, None)
    ok = bool(Quartz.CGImageDestinationFinalize(dest))
    return ok, w, h


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    pdf, png = sys.argv[1], sys.argv[2]
    scale = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    ok, w, h = render(pdf, png, scale)
    print("%s -> %s | %dx%d (%gx) | %d 字节"
          % (pdf, png, w, h, scale, os.path.getsize(png) if ok and os.path.exists(png) else 0))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
