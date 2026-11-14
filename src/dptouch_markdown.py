#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版说明用的轻量 Markdown 渲染。

只处理 GitHub Release 正文里真实会出现的语法: 标题、粗体、行内代码、链接、
列表、引用、代码块、分隔线、表格行。不追求完整 CommonMark —— 目标是把
`##` `**` 这类标记变成能读的富文本, 而不是原样露出来。
"""

import re

from AppKit import (
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSLinkAttributeName,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
)
from Foundation import NSURL

BODY_SIZE = 12.0
HEAD_SIZES = {1: 16.0, 2: 14.0, 3: 13.0, 4: 12.5, 5: 12.0, 6: 12.0}

_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\n]+\))")


def _font(size=BODY_SIZE, bold=False, mono=False):
    if mono:
        return NSFont.monospacedSystemFontOfSize_weight_(size - 0.5, 0.0)
    if bold:
        return NSFont.boldSystemFontOfSize_(size)
    return NSFont.systemFontOfSize_(size)


def _para(before=0.0, after=2.0, first=0.0, head=0.0):
    p = NSMutableParagraphStyle.alloc().init()
    p.setParagraphSpacing_(after)
    p.setParagraphSpacingBefore_(before)
    p.setFirstLineHeadIndent_(first)
    p.setHeadIndent_(head)
    return p


def _attrs(size=BODY_SIZE, bold=False, mono=False, color=None, para=None):
    a = {NSFontAttributeName: _font(size, bold, mono)}
    if color is not None:
        a[NSForegroundColorAttributeName] = color
    if para is not None:
        a[NSParagraphStyleAttributeName] = para
    return a


def _text_color():
    from AppKit import NSColor
    return NSColor.labelColor()


def _dim_color():
    from AppKit import NSColor
    return NSColor.secondaryLabelColor()


def _link_color():
    from AppKit import NSColor
    return NSColor.linkColor()


def _append(out, s, attrs):
    if not s:
        return
    out.appendAttributedString_(
        NSMutableAttributedString.alloc().initWithString_attributes_(s, attrs))


def _append_inline(out, s, base, bold_font=None, mono_font=None):
    """行内: **粗体** / `代码` / [文字](链接)。"""
    pos = 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            _append(out, s[pos:m.start()], base)
        tok = m.group(0)
        if tok.startswith("**"):
            a = dict(base)
            a[NSFontAttributeName] = bold_font or _font(BODY_SIZE, True)
            _append(out, tok[2:-2], a)
        elif tok.startswith("`"):
            a = dict(base)
            a[NSFontAttributeName] = mono_font or _font(BODY_SIZE, mono=True)
            _append(out, tok[1:-1], a)
        else:
            mm = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok)
            if mm is None:
                _append(out, tok, base)
                pos = m.end()
                continue
            a = dict(base)
            try:
                a[NSLinkAttributeName] = NSURL.URLWithString_(mm.group(2))
                a[NSForegroundColorAttributeName] = _link_color()
            except Exception:
                pass
            _append(out, mm.group(1), a)
        pos = m.end()
    if pos < len(s):
        _append(out, s[pos:], base)


def to_attributed(text):
    """把发版正文转成 NSAttributedString（无换行结尾的段落序列）。"""
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", src)          # 图片直接去掉
    src = re.sub(r"<[^>\n]{1,80}>", "", src)                 # 裸 HTML 标签
    lines = src.split("\n")

    out = NSMutableAttributedString.alloc().init()
    in_code = False
    code_buf = []

    def flush_code():
        if not code_buf:
            return
        a = _attrs(size=BODY_SIZE, mono=True, color=_text_color(),
                   para=_para(2.0, 4.0, 10.0, 10.0))
        _append(out, "\n".join(code_buf), a)
        _append(out, "\n", a)
        code_buf.clear()

    for raw in lines:
        line = raw.rstrip()

        fence = line.strip().startswith("```")
        if fence:
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if not line.strip():
            _append(out, "\n", _attrs(para=_para(0.0, 4.0)))
            continue

        # 分隔线
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            _append(out, "\n", _attrs(para=_para(2.0, 4.0)))
            continue

        # 标题
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            size = HEAD_SIZES.get(level, BODY_SIZE)
            base = _attrs(size=size, bold=True, color=_text_color(),
                          para=_para(6.0 if level <= 2 else 4.0, 2.0))
            _append_inline(out, m.group(2).strip(), base, bold_font=_font(size, True))
            _append(out, "\n", base)
            continue

        # 引用
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            base = _attrs(color=_dim_color(), para=_para(0.0, 2.0, 12.0, 12.0))
            _append_inline(out, m.group(1), base)
            _append(out, "\n", base)
            continue

        # 列表
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            base = _attrs(color=_text_color(), para=_para(0.0, 2.0, 16.0, 16.0))
            _append(out, "• ", base)
            _append_inline(out, m.group(2), base)
            _append(out, "\n", base)
            continue
        m = re.match(r"^(\s*)(\d+\.)\s+(.*)$", line)
        if m:
            base = _attrs(color=_text_color(), para=_para(0.0, 2.0, 16.0, 16.0))
            _append(out, m.group(2) + " ", base)
            _append_inline(out, m.group(3), base)
            _append(out, "\n", base)
            continue

        # 表格行: 用 · 分隔, 去掉对齐行
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                continue
            base = _attrs(color=_text_color(), para=_para(0.0, 2.0))
            _append_inline(out, " · ".join(c for c in cells if c), base)
            _append(out, "\n", base)
            continue

        # 普通段落
        base = _attrs(color=_text_color(), para=_para(0.0, 3.0))
        _append_inline(out, line.strip(), base)
        _append(out, "\n", base)

    if in_code:
        flush_code()
    return out


if __name__ == "__main__":
    import sys
    s = to_attributed(open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1
                      else "## 标题\n- **粗体** 与 `代码`\n[链接](https://example.com)\n")
    print(s.string()[:400])
