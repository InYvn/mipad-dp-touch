#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 许愿工坊的 AppKit 页面与设置面板。

页面本身不做请求：只读 `app.ai` 的状态并渲染；点按钮走
`SettingsWindow` 上的 selector（onAISend_ 等），和现有页面一致。
"""

import objc
from Foundation import NSObject
from AppKit import (NSApplication, NSColor, NSFont, NSMakeRect,
                    NSAttributedString, NSMutableAttributedString,
                    NSMutableParagraphStyle,
                    NSPanel, NSPopUpButton, NSScrollView, NSSecureTextField,
                    NSTextField, NSTextView, NSRange,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSParagraphStyleAttributeName, NSWindowStyleMaskTitled,
                    NSWindowStyleMaskClosable, NSBackingStoreBuffered)

import ai_providers as PR

HEADER_FONT = 12.5
BODY_FONT = 12.5


def _color(name, fallback=None):
    try:
        return getattr(NSColor, name)()
    except Exception:
        return fallback or NSColor.labelColor()


def _mono(size=12.0):
    try:
        return NSFont.monospacedSystemFontOfSize_weight_(size, 0.0)
    except Exception:
        return NSFont.systemFontOfSize_(size)


def _text_view(rect, editable=False):
    tv = NSTextView.alloc().initWithFrame_(rect)
    tv.setEditable_(bool(editable))
    tv.setSelectable_(True)
    tv.setRichText_(True)
    tv.setDrawsBackground_(False)
    tv.setFont_(_mono(BODY_FONT))
    tv.setTextColor_(_color("labelColor"))
    tv.setVerticallyResizable_(True)
    tv.setHorizontallyResizable_(False)
    tv.textContainer().setWidthTracksTextView_(True)
    tv.setAutoresizingMask_(2)          # width sizable
    tv.textContainer().setLineFragmentPadding_(4.0)
    return tv


def _scroll(win, rect, tv):
    sc = NSScrollView.alloc().initWithFrame_(rect)
    sc.setBorderType_(0)
    sc.setDrawsBackground_(False)
    sc.setHasVerticalScroller_(True)
    sc.setAutohidesScrollers_(True)
    sc.setDocumentView_(tv)
    return sc


def _attr(text, size=BODY_FONT, bold=False, color=None, mono=False):
    f = _mono(size) if mono else (NSFont.boldSystemFontOfSize_(size) if bold
                                  else NSFont.systemFontOfSize_(size))
    return (text, {NSFontAttributeName: f,
                   NSForegroundColorAttributeName: color or _color("labelColor")})


def _conversation_text(ai):
    """把对话渲染成 NSAttributedString（角色用颜色区分）。"""
    out = NSMutableAttributedString.alloc().init()
    para = NSMutableParagraphStyle.alloc().init()
    para.setLineSpacing_(2.0)
    body_attrs = {NSParagraphStyleAttributeName: para}
    if not ai.messages:
        out.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                "还没有对话。\n\n试试：「把笔的轻点改成右键」「滚动速度调慢一点」\n"
                "做不到的会直接说做不到，不会假装改成功。",
                dict(body_attrs, **{NSFontAttributeName: NSFont.systemFontOfSize_(BODY_FONT),
                                    NSForegroundColorAttributeName: _color("secondaryLabelColor")})))
        return out
    for m in ai.messages:
        role = m.get("role")
        ts = m.get("ts") or ""
        if role == "user":
            name, col = "你", _color("labelColor")
        elif role == "assistant":
            name, col = "AI", NSColor.systemBlueColor()
        else:
            name, col = "系统", (_color("systemRedColor") if m.get("kind") == "error"
                                else _color("secondaryLabelColor"))
        head = "%s  %s\n" % (name, ts)
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            head, dict(body_attrs, **{NSFontAttributeName: NSFont.boldSystemFontOfSize_(11.5),
                                      NSForegroundColorAttributeName: col})))
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            str(m.get("text") or "") + "\n\n",
            dict(body_attrs, **{NSFontAttributeName: NSFont.systemFontOfSize_(BODY_FONT),
                                NSForegroundColorAttributeName:
                                    _color("systemRedColor") if m.get("kind") == "error"
                                    else _color("labelColor")})))
    return out


def build(win, v, x, w, y):
    """在内容区画 AI 页；返回新的 y（top-origin）。"""
    from dptouch_window import _label, _button, _line_view, brand_color
    ai = getattr(win.app, "ai", None)

    y = win._eyebrow(v, x, y, "AI WORKSHOP / 06")
    y = win._page_head(v, x, w, y, "AI 许愿工坊",
                       "用一句话改设置：先预览差异，确认后生效，随时可撤销。")

    # ---- 状态行 + 设置入口
    st = ai.status_text() if ai else "不可用"
    col = (_color("systemGreenColor") if ai and ai.has_key() and ai.state != "error"
           else _color("systemOrangeColor") if ai and not ai.has_key()
           else _color("systemRedColor") if ai and ai.state == "error"
           else _color("secondaryLabelColor"))
    win.c["ai_status"] = _label(st, win._r(x, y, w - 130, 18), size=12, color=col)
    v.addSubview_(win.c["ai_status"])
    win.c["ai_settings_btn"] = _button("AI 设置…", win._r(x + w - 118, y - 4, 118, 26),
                                       win, "onAISettings:", small=True)
    v.addSubview_(win.c["ai_settings_btn"])
    y += 26

    # ---- 对话区
    conv_h = 320.0
    win._card(v, y, conv_h, x, w)
    tv = _text_view(NSMakeRect(0, 0, w - 28, conv_h - 16), editable=False)
    if ai:
        tv.textStorage().setAttributedString_(_conversation_text(ai))
    sc = _scroll(win, win._r(x + 10, y + 8, w - 20, conv_h - 16), tv)
    v.addSubview_(sc)
    win.c["ai_conv"] = tv
    win.c["ai_conv_scroll"] = sc
    y += conv_h + 10

    # ---- 操作按钮
    by = y
    pending = bool(ai and ai.state == "proposed")
    busy = bool(ai and ai.busy())
    win.c["ai_exec"] = _button("执行改动", win._r(x, by, 96, 26), win, "onAIExec:", small=True)
    win.c["ai_exec"].setEnabled_(pending)
    v.addSubview_(win.c["ai_exec"])
    win.c["ai_cancel"] = _button("取消", win._r(x + 104, by, 72, 26), win, "onAICancel:", small=True)
    win.c["ai_cancel"].setEnabled_(pending)
    v.addSubview_(win.c["ai_cancel"])
    win.c["ai_undo"] = _button("撤销上一次", win._r(x + 184, by, 104, 26), win, "onAIUndo:", small=True)
    win.c["ai_undo"].setEnabled_(not busy)
    v.addSubview_(win.c["ai_undo"])
    win.c["ai_clear"] = _button("清空对话", win._r(x + 296, by, 84, 26), win, "onAIClear:", small=True)
    v.addSubview_(win.c["ai_clear"])
    hint = ("待确认：点「执行改动」后才会写配置" if pending else
            "正在请求模型…" if (ai and ai.state == "thinking") else
            "改动会先列出来，确认后才生效")
    win.c["ai_hint"] = _label(hint, win._r(x + 392, by + 4, w - 392, 16), size=11, dim=True)
    v.addSubview_(win.c["ai_hint"])
    y += 36

    # ---- 输入区
    v.addSubview_(_label("你想改什么？", win._r(x, y, w, 16), size=11, dim=True))
    y += 20
    in_h = 64.0
    win._card(v, y, in_h, x, w)
    itv = _text_view(NSMakeRect(0, 0, w - 28, in_h - 12), editable=True)
    itv.setFont_(NSFont.systemFontOfSize_(BODY_FONT))
    itv.setString_(getattr(ai, "_draft", "") if ai else "")
    isc = _scroll(win, win._r(x + 10, y + 6, w - 20, in_h - 12), itv)
    v.addSubview_(isc)
    win.c["ai_input"] = itv
    y += in_h + 8

    win.c["ai_send"] = _button("发送", win._r(x, y, 84, 28), win, "onAISend:")
    win.c["ai_send"].setEnabled_(bool(ai) and not busy)
    v.addSubview_(win.c["ai_send"])
    win.c["ai_stop"] = _button("停止", win._r(x + 92, y, 84, 28), win, "onAIStop:")
    win.c["ai_stop"].setEnabled_(bool(ai and ai.state == "thinking"))
    v.addSubview_(win.c["ai_stop"])
    v.addSubview_(_label("回车发送 · 只发送你的这句话和公开设置项，不发日志/路径/设备信息",
                         win._r(x + 190, y + 6, w - 190, 16), size=10, dim=True))
    y += 34
    return y


def refresh(win, force=False):
    """就地更新（不重建视图，避免输入框丢焦点）。"""
    ai = getattr(win.app, "ai", None)
    if ai is None:
        return
    try:
        tv = win.c.get("ai_conv")
        if tv is not None:
            tv.textStorage().setAttributedString_(_conversation_text(ai))
            n = tv.string().length() if hasattr(tv.string(), "length") else len(tv.string())
            tv.scrollRangeToVisible_(NSRange(n, 0))
        lb = win.c.get("ai_status")
        if lb is not None:
            lb.setStringValue_(ai.status_text())
        for key, enabled in (("ai_exec", ai.state == "proposed"),
                             ("ai_cancel", ai.state == "proposed"),
                             ("ai_undo", not ai.busy()),
                             ("ai_send", not ai.busy()),
                             ("ai_stop", ai.state == "thinking")):
            b = win.c.get(key)
            if b is not None:
                b.setEnabled_(bool(enabled))
        h = win.c.get("ai_hint")
        if h is not None:
            h.setStringValue_("待确认：点「执行改动」后才会写配置" if ai.state == "proposed"
                              else "正在请求模型…" if ai.state == "thinking"
                              else "改动会先列出来，确认后才生效")
    except Exception as e:
        try:
            win.app.log("AI 页刷新失败: %r" % (e,))
        except Exception:
            pass


# ---------------------------------------------------------------- 设置面板

class AISettingsPanel(NSObject):
    """Provider / Base URL / 模型 / 密钥 / 超时。密钥进钥匙串，不进配置。"""

    def init(self):
        self = objc.super(AISettingsPanel, self).init()
        if self is None:
            return None
        self.win = None
        self.panel = None
        self.f_provider = None
        self.f_base = None
        self.f_model = None
        self.f_key = None
        self.f_timeout = None
        return self

    @objc.python_method
    def show(self, win):
        from dptouch_window import _label, _button
        self.win = win
        ai = win.app.ai
        s = ai.settings()
        W, H = 500.0, 300.0
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        p.setTitle_("AI 设置")
        p.setReleasedWhenClosed_(False)
        v = p.contentView()

        def lab(text, x, y, w=110, size=12):
            lb = _label(text, NSMakeRect(x, H - y - 18, w, 18), size=size)
            v.addSubview_(lb)
            return lb

        lab("Provider", 20, 22)
        self.f_provider = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(140, H - 26, 330, 26), False)
        self.f_provider.addItemsWithTitles_([PR.PROVIDERS[k]["title"] for k in PR.provider_ids()])
        ids = PR.provider_ids()
        if s["provider"] in ids:
            self.f_provider.selectItemAtIndex_(ids.index(s["provider"]))
        self.f_provider.setTarget_(self)
        self.f_provider.setAction_("providerChanged:")
        v.addSubview_(self.f_provider)

        lab("Base URL", 20, 62)
        self.f_base = NSTextField.alloc().initWithFrame_(NSMakeRect(140, H - 66, 330, 24))
        self.f_base.setStringValue_(s["base_url"] or "")
        self.f_base.setPlaceholderString_(PR.PROVIDERS[s["provider"]]["base"])
        v.addSubview_(self.f_base)

        lab("模型", 20, 102)
        self.f_model = NSTextField.alloc().initWithFrame_(NSMakeRect(140, H - 106, 330, 24))
        self.f_model.setStringValue_(s["model"] or "")
        self.f_model.setPlaceholderString_(PR.PROVIDERS[s["provider"]]["model"])
        v.addSubview_(self.f_model)

        lab("API Key", 20, 142)
        self.f_key = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(140, H - 146, 330, 24))
        self.f_key.setPlaceholderString_("已保存则留空；本机模型可留空")
        v.addSubview_(self.f_key)

        lab("超时（秒）", 20, 182)
        self.f_timeout = NSTextField.alloc().initWithFrame_(NSMakeRect(140, H - 186, 90, 24))
        self.f_timeout.setStringValue_(str(s["timeout"]))
        v.addSubview_(self.f_timeout)

        hint = _label("密钥存进 macOS 钥匙串，不写配置文件、不进日志。",
                      NSMakeRect(20, 34, 460, 16), size=10, dim=True)
        v.addSubview_(hint)

        ok = _button("保存", NSMakeRect(W - 190, 12, 84, 28), self, "save:")
        ca = _button("取消", NSMakeRect(W - 100, 12, 84, 28), self, "cancel:")
        v.addSubview_(ok)
        v.addSubview_(ca)

        self.panel = p
        p.center()
        NSApplication.sharedApplication().runModalForWindow_(p)

    def providerChanged_(self, sender):
        ids = PR.provider_ids()
        i = self.f_provider.indexOfSelectedItem()
        if 0 <= i < len(ids):
            m = PR.PROVIDERS[ids[i]]
            self.f_base.setPlaceholderString_(m["base"])
            self.f_model.setPlaceholderString_(m["model"])

    def save_(self, sender):
        try:
            ai = self.win.app.ai
            ids = PR.provider_ids()
            i = self.f_provider.indexOfSelectedItem()
            pid = ids[max(0, min(i, len(ids) - 1))]
            base = self.f_base.stringValue().strip()
            model = self.f_model.stringValue().strip()
            try:
                timeout = int(self.f_timeout.stringValue().strip() or "120")
            except Exception:
                timeout = 120
            ai.save_settings(provider=pid, base_url=base, model=model, timeout=timeout)
            key = self.f_key.stringValue().strip()
            if key:
                ai.set_key(key, pid)
        finally:
            try:
                NSApplication.sharedApplication().stopModal()
                self.panel.close()
            except Exception:
                pass
            try:
                self.win._relayout(animate=False)
            except Exception:
                pass

    def cancel_(self, sender):
        try:
            NSApplication.sharedApplication().stopModal()
            self.panel.close()
        except Exception:
            pass
