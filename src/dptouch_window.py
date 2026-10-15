#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置窗口 —— 看得见的主界面 + 分步权限引导。

为什么要这个窗口(而不是继续把东西塞进菜单):
一个"能直接安装的图形化程序"应当有一个看得见的界面。菜单栏应用的惯例是 ——
菜单只放**常用开关**, 所有设置、状态、权限说明都进窗口。之前菜单里有 20 多项、
每项后面挂一句解释, 于是菜单读起来像 changelog, 权限说明是一整面弹窗文字。

这一版的三条硬规矩:

1. **菜单与窗口是同一份状态的两个表面。** 两边都只调 ``app.actXxx_``, 都从 app 的
   状态渲染。程序化写控件(``setState_`` / ``selectItemAtIndex_``)时必须挂
   ``_syncing`` 挡板 —— 否则设置控件的动作会反过来写一遍状态, 跟用户的手抢。
2. **权限分步。** 一次只让用户做一件事: 上一步没完成时, 下一步是灰的, 并写明"上一步
   完成后才能进行"。这样就不会一上来被两个系统弹窗同时轰。
3. **手算 frame, 不用 Auto Layout / NSStackView。** 布局是固定的, 手算二十行就够,
   还避开了 macOS 大版本之间 Auto Layout 常量改名(运行时才炸)的坑。

GUI 是"我改我看不见"的东西, 所以布局给了离线质检: ``check_layout()`` 在窗口无头
自检里跑, 报控件越界 / 互相重叠 / 文字被切。见 ``dptouch.py`` 的
``DPTOUCH_SELFTEST_WINDOW=exit``。
"""

import os

import objc
from AppKit import (
    NSAffineTransform, NSAttributedString, NSBackingStoreBuffered, NSBox, NSBundle, NSButton,
    NSColor, NSFont, NSGradient, NSGraphicsContext, NSShadow,
    NSFontAttributeName, NSForegroundColorAttributeName, NSImage, NSImageView,
    NSMakePoint, NSMakeRect, NSMakeSize,
    NSObject, NSPopUpButton, NSTrackingArea,
    NSTextField, NSWindow, NSWorkspace,
    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled,
)

try:                                        # 新 SDK 里叫 NSButtonTypeSwitch
    from AppKit import NSButtonTypeSwitch as _BTN_SWITCH
except Exception:                           # pragma: no cover
    _BTN_SWITCH = 3


# 菜单栏 App 弹出窗口时, App 并没有被激活 —— 窗口是"未激活"的样子: 系统会把
# 下拉框文字画成灰的, 而且第一次点击只用来激活 App、不落到控件上(用户看到的就是
# "选项是灰的、点了没反应、勾选又弹回去")。这两个子类让控件直接吃下第一击;
# 抢焦点另见 SettingsWindow._activate()。
class _FirstMousePopup(NSPopUpButton):
    def acceptsFirstMouse_(self, event):
        return True


class _FirstMouseButton(NSButton):
    def acceptsFirstMouse_(self, event):
        return True

from AppKit import NSBezierPath, NSView

# 分组底: 深色模式下比窗口底稍亮的一层圆角块, 用来替"一堆粗体小标题"做分组。
CARD_R = 10.0


class _CardView(NSView):
    """一块圆角分组底(纯装饰, 不接收点击)。

    ``tint`` 传系统颜色名时画成"高亮行"(增强模式那一行); ``tint_alpha`` 控制深浅
    —— 用颜色自带的 alpha, 不靠视图的 setAlphaValue_ (那个在无层视图上不稳)。

    ★golden-gate 之后: 整页不再一层层套圆角卡片 (那是"把网页仪表盘塞进 Mac 窗口"),
    这个类只留给两处有语义的边界 —— 权限引导块(会消失的临时块) 和 增强模式那一行
    的**行状态**高亮。
    """
    tint = None
    tint_alpha = 1.0
    _hover = False               # 悬停时整行提一点亮 (heyimjames: 可点的地方都要有 hover)

    def drawRect_(self, rect):
        b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), CARD_R, CARD_R)
        c = _color(self.tint or "controlBackgroundColor")
        a = self.tint_alpha
        if self.tint and self._hover:
            a = min(1.0, a + 0.06)
        if self.tint and a < 1.0:
            c = c.colorWithAlphaComponent_(a)
        c.set()
        b.fill()


class _HoverTint(_CardView):
    """可悬停的高亮行: 鼠标进来时底色淡入淡出 (0.12s easeOut —— 慢了就显得迟钝)。"""

    def updateTrackingAreas(self):
        for t in list(self.trackingAreas() or []):
            self.removeTrackingArea_(t)
        opt = (0x01 | 0x20 | 0x200)      # MouseEnteredAndExited | ActiveInKeyWindow | InVisibleRect
        self.addTrackingArea_(NSTrackingArea.alloc()
                              .initWithRect_options_owner_userInfo_(NSMakeRect(0, 0, 0, 0),
                                                                    opt, self, None))
        objc.super(_HoverTint, self).updateTrackingAreas()   # 不能用裸 super(): PyObjC 会 trap

    def mouseEntered_(self, ev):
        self._set_hover(True)

    def mouseExited_(self, ev):
        self._set_hover(False)

    def _set_hover(self, on):
        self._hover = bool(on)
        try:
            from AppKit import NSAnimationContext
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(0.12)
            NSAnimationContext.endGrouping()
        except Exception:
            pass
        self.setNeedsDisplay_(True)


def _line_view(rect):
    """原生分隔线 (NSBox 的 separator 样式) —— AppKit 自己按外观/深浅画发丝线。"""
    b = NSBox.alloc().initWithFrame_(rect)
    b.setBoxType_(2)                 # NSBoxSeparator
    b.setIdentifier_("line")         # 布局自检跳过装饰件 (同 "bg")
    return b


def reduce_motion():
    """系统「减弱动态效果」开关。开了就不做动画 (golden-gate 的硬要求)。"""
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


import hid_bridge as HB
from hid_bridge import HOLD_MS_OPTS as _HOLD_MS   # 「停住再滑」判定档位(ms), 与 CLI 同源
from hid_bridge import RC_HOLD_MS_OPTS as _RC_HOLD   # 「停住不动」判定档位(ms), 与 CLI 同源

# ---------------------------------------------------------------- 外壳尺寸 / 调色板
# 依据: Downloads/Mipad_DP_Touch_Design_Prototype.html (侧边栏 + 多页版式)。
# 颜色尽量走语义色 (浅色/深色/失焦自动跟随); 只有品牌色是写死的 —— 它来自 logo,
# 不跟随系统强调色 (golden-gate: 品牌色可以显式 opt-out, 其余用语义色)。
W, H = 980.0, 700.0                         # 主窗口内容尺寸(固定; 不设 Resizable)
SIDEBAR_W = 220.0                           # 侧边栏宽
TOPBAR_H = 0.0                              # 顶部工具行已取消（名称/连接状态/图标都并进侧栏）
STRIP_H = 38.0                              # 底部状态条
NAV_H = 39.0                                # 侧栏导航项高度
NAV_ICON = 17.0                             # 导航图标边长
CONTENT_MAX = 810.0                         # 内容区最大宽 (设计稿: max-width 810, 居中)
BRAND = (0.929, 0.416, 0.341)               # #ed6a57 —— logo 的橙红


def rgba(r, g, b, a=1.0):
    """固定字面色 (插画用)。★不要拿动态系统色再 colorWithAlphaComponent_ 塞进 NSGradient
    —— alpha 会被丢掉, 屏幕会画成饱和色块 (实测: 淡蓝紫粉变成青绿)。"""
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def brand_color(alpha=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(BRAND[0], BRAND[1], BRAND[2], alpha)


def _icon(name, size=NAV_ICON, weight=0.0, color=None):
    """SF Symbols 图标。取不到就返回 None (调用方按纯文字排, 不留空洞)。"""
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return None
        try:
            from AppKit import NSImageSymbolConfiguration
            img = img.imageWithSymbolConfiguration_(
                NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight))
        except Exception:
            pass
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
        iv.setImage_(img)
        iv.setContentTintColor_(color if color is not None else _color("secondaryLabelColor"))
        iv.setIdentifier_("ico")             # 布局自检跳过 (纯图形, 没有文字宽度可比)
        return iv
    except Exception:
        return None


class _NavButton(NSButton):
    """侧栏导航项: 图标 + 文字 (+ 徽标由调用方另放)。

    悬停浅底、选中灰底 —— 都自己画 (设计稿的 .nav button:hover/.active)。
    用 NSButton 而不是裸 NSView: 点击/目标/动作、首击、键盘可达全是现成的。
    """
    key = ""
    _hover = False

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        for t in list(self.trackingAreas() or []):
            self.removeTrackingArea_(t)
        self.addTrackingArea_(NSTrackingArea.alloc()
                              .initWithRect_options_owner_userInfo_(NSMakeRect(0, 0, 0, 0),
                                                                    0x01 | 0x20 | 0x200,
                                                                    self, None))
        objc.super(_NavButton, self).updateTrackingAreas()

    def mouseEntered_(self, ev):
        self._hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, ev):
        self._hover = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self.state():
            _color("unemphasizedSelectedContentBackgroundColor").set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 8.0, 8.0).fill()
        elif self._hover:
            _color("labelColor").colorWithAlphaComponent_(0.07).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 8.0, 8.0).fill()
        objc.super(_NavButton, self).drawRect_(rect)


class _Switch(NSButton):
    """自绘开关 (36×22) —— 设计稿的 .switch。原生 NSSwitch 尺寸是 26×15, 对不上。

    画法: 圆角胶囊底 + 白色圆钮; 开=强调色。点击/状态/动作仍由 NSButton 负责。
    """

    def drawRect_(self, rect):
        b = self.bounds()
        on = bool(self.state())
        r = b.size.height / 2.0
        (_color("controlAccentColor") if on else _color("quaternaryLabelColor")).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, r, r).fill()
        d = b.size.height - 4.0
        x = (b.size.width - d - 2.0) if on else 2.0
        _color("whiteColor").set()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, 2.0, d, d)).fill()


class _IconButton(NSButton):
    """工具行/行尾的小图标按钮: 悬停浅底 (设计稿 .iconbtn)。"""

    _hover = False

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        for t in list(self.trackingAreas() or []):
            self.removeTrackingArea_(t)
        self.addTrackingArea_(NSTrackingArea.alloc()
                              .initWithRect_options_owner_userInfo_(NSMakeRect(0, 0, 0, 0),
                                                                    0x01 | 0x20 | 0x200,
                                                                    self, None))
        objc.super(_IconButton, self).updateTrackingAreas()

    def mouseEntered_(self, ev):
        self._hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, ev):
        self._hover = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self._hover:
            _color("labelColor").colorWithAlphaComponent_(0.08).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 7.0, 7.0).fill()
        objc.super(_IconButton, self).drawRect_(rect)


class _Fill(NSView):
    """一块纯色底 (侧栏 / 内容区 / 分隔条)。fill 给语义色名, 或直接给 NSColor。"""

    fill = "windowBackgroundColor"

    def drawRect_(self, rect):
        c = self.fill if hasattr(self.fill, "set") else _color(self.fill)
        c.set()
        NSBezierPath.fillRect_(self.bounds())


def _fill_view(rect, fill, ident="bg"):
    b = _Fill.alloc().initWithFrame_(rect)
    b.fill = fill
    b.setIdentifier_(ident)              # 装饰件: 布局自检跳过 (同卡片底/分隔线)
    return b


class _DeviceArt(NSView):
    """侧栏里的设备图: 斜放的平板 + 触控笔 + 屏幕涂鸦 + 一圈暖色光晕。

    照设计稿 .device-graphic / .tablet-art / .stylus 画的, 全矢量 —— 位图在浅色/深色
    下要出两套, 矢量跟着语义色走。未连接时整体压暗 (on=False)。
    """
    TW, TH = 146.0, 100.0       # 机身比例按真机 296.2×203.1 (1.46:1) 缩到侧栏宽度
    BEZ = 3.7                   # 边框: 真机 7.6mm / 296.2mm × 146 ≈ 3.7pt (屏占比 87.6%)
    RAD = 5.0                   # 圆角: 真机 ~10mm -> 5pt
    ROT = -7.0                  # 平板斜放角度
    PEN_L, PEN_W, PEN_ROT = 96.0, 5.5, 42.0     # 触控笔
    on = True

    def content_box(self):
        """画出来的东西**实际**占多大 (旋转后的外接矩形, 视图坐标系)。

        ★NSView 默认不裁剪绘制: 画到框外照样画出来, 会压住上下两行文字 ——
        而布局审计只看 frame, 看不见"画到框外"。所以把真实占位算出来给侧栏排,
        再在自检里断言它没超出 bounds。
        """
        import math
        r = math.radians(abs(self.ROT))
        bw = self.TW * math.cos(r) + self.TH * math.sin(r)
        bh = self.TH * math.cos(r) + self.TW * math.sin(r)
        pr = math.radians(self.PEN_ROT)
        pw = self.PEN_L * math.sin(pr) + self.PEN_W * math.cos(pr)
        ph = self.PEN_L * math.cos(pr) + self.PEN_W * math.sin(pr)
        w, h = self.bounds().size.width, self.bounds().size.height
        cx, cy = w / 2.0, h / 2.0                       # 平板居中
        px, py = w / 2.0 + 44.0, h / 2.0                # 笔在右侧
        x0 = min(cx - bw / 2.0, px - pw / 2.0)
        x1 = max(cx + bw / 2.0, px + pw / 2.0)
        y0 = min(cy - bh / 2.0, py - ph / 2.0)
        y1 = max(cy + bh / 2.0, py + ph / 2.0)
        return (x0, y0, x1 - x0, y1 - y0)

    def _scribble(self):
        """屏幕上那笔涂鸦 (设计稿的 SVG 路径, 72×52 坐标; AppKit 的 y 是反的, 调用方已翻)。"""
        p = NSBezierPath.bezierPath()
        p.setLineWidth_(4.0)
        try:
            p.setLineCapStyle_(1)               # round
            p.setLineJoinStyle_(1)
        except Exception:
            pass
        p.moveToPoint_(NSMakePoint(6, 30))
        p.curveToPoint_controlPoint1_controlPoint2_(
            NSMakePoint(24, 33), NSMakePoint(22, 1), NSMakePoint(34, 15))
        p.curveToPoint_controlPoint1_controlPoint2_(
            NSMakePoint(32, 33), NSMakePoint(16, 57), NSMakePoint(32, 45))
        p.curveToPoint_controlPoint1_controlPoint2_(
            NSMakePoint(55, 23), NSMakePoint(23, 33), NSMakePoint(35, 10))
        p.curveToPoint_controlPoint1_controlPoint2_(
            NSMakePoint(67, 29), NSMakePoint(63, 13), NSMakePoint(68, 16))
        return p

    def drawRect_(self, rect):
        b = self.bounds()
        cw, ch = b.size.width, b.size.height
        if not self.on:
            alpha = 0.45                        # 没连上 -> 压暗, 不抢视线
        else:
            alpha = 1.0
        NSGraphicsContext.saveGraphicsState()
        try:
            # ---- 1) 暖色光晕 (radial, 中心偏下) ----
            if self.on:
                glow = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(cw * 0.06, ch * 0.02, cw * 0.88, ch * 0.92))
                NSGradient.alloc().initWithColors_(
                    [rgba(0.945, 0.431, 0.337, 0.20 * alpha),
                     rgba(0.945, 0.431, 0.337, 0.0)]
                ).drawInBezierPath_relativeCenterPosition_(glow, NSMakePoint(0.0, -0.1))
            # ---- 2) 平板 (161×104, 圆角 12, 边框 6, 转 -7°) ----
            tw, th, bez = self.TW, self.TH, self.BEZ
            cx, cy = cw / 2.0, ch / 2.0
            NSGraphicsContext.saveGraphicsState()        # ★变换必须配对 save/restore:
            t = NSAffineTransform.transform()            #   少一个 restore 就把外层状态弹穿,
            t.translateXBy_yBy_(cx, cy)                  #   进程直接 trap (drawRect_ 跑完没事,
            t.rotateByDegrees_(-7.0)                     #   死在随后的 restoreGraphicsState)
            t.concat()
            outer = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(-tw / 2.0, -th / 2.0, tw, th), self.RAD, self.RAD)
            sh = NSShadow.alloc().init()
            sh.setShadowOffset_(NSMakeSize(0.0, -4.0))
            sh.setShadowBlurRadius_(10.0)
            sh.setShadowColor_(rgba(0.133, 0.149, 0.192, 0.14 * alpha))
            sh.set()
            rgba(0.208, 0.224, 0.255, alpha).set()                      # #353941 机身边框
            outer.fill()                                            # 机身边框
            inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(-tw / 2.0 + bez, -th / 2.0 + bez, tw - 2 * bez, th - 2 * bez),
                self.RAD - 1.8, self.RAD - 1.8)
            try:
                NSGradient.alloc().initWithColors_(
                    [rgba(0.914, 0.957, 0.988, alpha),                 # #e9f4fc
                     rgba(0.941, 0.929, 0.969, alpha),                 # #f0edf7
                     rgba(0.988, 0.863, 0.812, alpha)]                 # #fcdccf
                ).drawInBezierPath_angle_(inner, -55.0)
            except Exception:
                rgba(0.94, 0.94, 0.97, alpha).set()
                inner.fill()
            rgba(0.62, 0.65, 0.70, 0.85 * alpha).set()                 # 金属中框的高光
            _fr = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(-tw / 2.0, -th / 2.0, tw, th), self.RAD, self.RAD)
            _fr.setLineWidth_(1.0)
            _fr.stroke()
            # ---- 真机细节: 顶部电源键 + 音量键, 底部 USB-C ----
            rgba(0.52, 0.55, 0.60, alpha).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(tw / 2.0 - 26.0, th / 2.0 - 1.4, 14.0, 1.6), 0.8, 0.8).fill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(tw / 2.0 - 8.0, th / 2.0 - 1.4, 6.0, 1.6), 0.8, 0.8).fill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(-5.0, -th / 2.0 - 0.4, 10.0, 1.8), 0.9, 0.9).fill()
            # ---- 屏幕上的斜向反光 (玻璃感) ----
            NSGraphicsContext.saveGraphicsState()
            inner.addClip()
            rgba(1.0, 1.0, 1.0, 0.30 * alpha).set()
            _hl = NSBezierPath.bezierPath()
            _hl.moveToPoint_(NSMakePoint(-tw / 2.0 - 10.0, th / 2.0 - 22.0))
            _hl.lineToPoint_(NSMakePoint(-tw / 2.0 + 46.0, th / 2.0))
            _hl.lineToPoint_(NSMakePoint(-tw / 2.0 + 74.0, th / 2.0))
            _hl.lineToPoint_(NSMakePoint(-tw / 2.0 + 18.0, th / 2.0 - 22.0))
            _hl.closePath()
            _hl.fill()
            NSGraphicsContext.restoreGraphicsState()
            # 屏幕上的涂鸦 (72×52 坐标 -> 左下角起, y 翻转)
            NSGraphicsContext.saveGraphicsState()
            sc = NSAffineTransform.transform()
            sc.translateXBy_yBy_(-tw / 2.0 + 43.0, -th / 2.0 + 8.0)
            sc.scaleXBy_yBy_(1.0, -1.0)
            sc.translateXBy_yBy_(0.0, -52.0)
            sc.concat()
            brand_color(0.85 * alpha).set()
            self._scribble().stroke()
            NSGraphicsContext.restoreGraphicsState()
            NSGraphicsContext.restoreGraphicsState()                 # 退出平板变换
            # ---- 3) 触控笔 (6×108, 转 42°) ----
            NSGraphicsContext.saveGraphicsState()
            st = NSAffineTransform.transform()
            st.translateXBy_yBy_(cw / 2.0 + 44.0, ch / 2.0)
            st.rotateByDegrees_(self.PEN_ROT)
            st.concat()
            pen = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(-self.PEN_W / 2.0, -self.PEN_L / 2.0, self.PEN_W, self.PEN_L),
                2.6, 2.6)
            try:
                NSGradient.alloc().initWithColors_(
                    [rgba(0.357, 0.392, 0.431, alpha),                 # #5b646e
                     rgba(0.110, 0.141, 0.173, alpha),                 # #1c242c
                     rgba(0.431, 0.467, 0.502, alpha)]                 # #6e7780
                ).drawInBezierPath_angle_(pen, 0.0)
            except Exception:
                rgba(0.2, 0.24, 0.28, alpha).set()
                pen.fill()
            nib = NSBezierPath.bezierPath()
            nib.moveToPoint_(NSMakePoint(-self.PEN_W / 2.0, -self.PEN_L / 2.0))
            nib.lineToPoint_(NSMakePoint(self.PEN_W / 2.0, -self.PEN_L / 2.0))
            nib.lineToPoint_(NSMakePoint(0.0, -self.PEN_L / 2.0 - 8.0))
            nib.closePath()
            rgba(0.224, 0.243, 0.271, alpha).set()                     # #393e45 笔尖
            nib.fill()
            NSGraphicsContext.restoreGraphicsState()
        except Exception:
            pass
        finally:
            NSGraphicsContext.restoreGraphicsState()


class _ToastView(NSView):
    """临时提示条 (设计稿的 .toast): 深色胶囊 + 白字, 2.3 秒后自己淡掉。"""

    text = ""

    def drawRect_(self, rect):
        _color("blackColor").colorWithAlphaComponent_(0.88).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10.0, 10.0).fill()
        a = NSAttributedString.alloc().initWithString_attributes_(
            self.text, {NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
                        NSForegroundColorAttributeName: _color("whiteColor")})
        sz = a.size()
        a.drawAtPoint_(NSMakePoint((self.bounds().size.width - sz[0]) / 2.0,
                                   (self.bounds().size.height - sz[1]) / 2.0))

HEAD_LOGO = 30.0                            # 状态行 logo 边长 (App 名字在标题栏里, 这里只做标识)
PERM_H_SLIM = 28.0                          # 权限完成后的单行高度
PERM_H_FULL = 132.0                         # 权限未完成时的分步引导块高度
AW, AH = 520.0, 312.0                       # 「高级选项」小窗口的内容尺寸（4 开关 + 3 下拉 + 检查更新/诊断 + 平板屏参数）
                                            # (多了末尾「立即检查更新」那一行 30pt)
M = 20.0                                    # 页面左右留白 (heyimjames 4pt 栅格: 窗口内容边 20pt)
PAD = 12.0                                  # 卡片内边距
LBL_W = 94.0                                # 标签列宽
CTRL_X = M + PAD + LBL_W                    # 下拉框那一列的左边缘 (126)
CTRL_W = 250.0                              # 下拉框标准宽
HINT_X = CTRL_X + CTRL_W + 8                # 行尾说明那列 (384)
CR = W - M - PAD                            # 卡片内容右边线 (588)
ROW_H = 28.0                                # 「标签 + 控件」一行的高度
CB_H = 26.0                                 # 复选框一行的高度
SEC_GAP = 20.0                              # 分组之间的间距 (heyimjames: 20pt 一节)

# UI 文案 —— 短句, 不用括号。菜单里不再出现这些选项, 这里是唯一出处。
NAT_LABELS = [("system", "跟随系统"), ("on", "始终自然"), ("off", "始终传统")]
HOLD_TITLES = ["%.2gs %s" % (ms / 1000.0, lab)
               for ms, lab in zip(_HOLD_MS, ("极快", "快", "标准", "慢"))]
RC_TITLES = ["关" if not ms else "%.1f 秒" % (ms / 1000.0) for ms in _RC_HOLD]

# 主窗口「笔的输入」那张表: 四类手势, 每一类一个下拉 (可绑的动作来自 hid_bridge 那张表,
# 唯一出处 —— 界面里出现的选项一定有实现)。
BIND_ROWS = tuple((g, title, HB.gesture_actions(g), d)
                  for g, title, _o, d in HB.BIND_GESTURES)
SCROLL_ROWS = (("nat", "滚动方向"), ("gain", "滚动速度"))

# 侧栏导航的四个页面 (设计稿: 设备概览 / 手势映射 / 显示与缩放 / 运行与诊断)
PAGES = (
    ("overview", "square.grid.2x2", "设备概览"),
    ("gestures", "hand.point.up", "手势映射"),
    ("display", "display", "显示与缩放"),
    ("runtime", "gearshape", "运行与诊断"),
)

# 高级选项 (不常用的): 控件键, 配置键, 标题, 动作
ADV_SWITCHES = (
    ("drag_pen", "drag_pen", "拖拽位置跟随笔尖", "onDragPen:"),
    ("unknown", "allow_unknown", "允许未验证的小米设备", "onUnknown:"),
    ("disp_allow", "display_allow_unknown", "允许管理未实测的显示器", "onDispAllow:"),
    # 只有这一项会让程序联网 (查 GitHub 上的最新版本); 关掉它就完全不联网
    ("update_auto", "update_auto", "自动检查更新", "onUpdateAuto:"),
)
ADV_POPUPS = (
    # 光标基准屏: 笔的绝对坐标铺到哪块屏上 —— 接了多块屏时靠它决定笔"在哪块屏上"工作
    ("target", "target_display", "光标基准屏", "onTarget:"),
    ("hold_ms", "hold_ms", "停住再滑判定", "onHoldMs:"),
    ("rc_hold", "rc_hold_ms", "停住不动判定", "onRcHold:"),
)

# 分步授权的两条 (顺序就是展示顺序)
PERM_STEPS = (
    ("ax", "辅助功能", "允许合成点击与拖拽，否则笔点了没反应"),
    ("input", "输入监控", "读取平板传过来的原始输入，否则收不到任何事件"),
)


# ---------------------------------------------------------------- 纯逻辑(可离线测)

def perm_steps(ax_ok, input_ok):
    """分步授权模型。

    state: done = 已完成; next = 现在该做这一步; wait = 前一步做完才轮到它。
    纯函数 —— 没有 AppKit 依赖, 所以可以拿单测钉住"一次只推进一步"这条规矩。
    """
    ok = {"ax": bool(ax_ok), "input": bool(input_ok)}
    order = [k for k, _, _ in PERM_STEPS]
    pending = [k for k in order if not ok[k]]
    nxt = pending[0] if pending else None
    out = []
    for i, (key, title, why) in enumerate(PERM_STEPS, 1):
        st = "done" if ok[key] else ("next" if key == nxt else "wait")
        out.append({"n": i, "key": key, "title": title, "why": why,
                    "state": st, "ok": ok[key]})
    return out


def perm_summary(ax_ok, input_ok):
    """(进度文字, 已完成数, 总数)"""
    st = perm_steps(ax_ok, input_ok)
    done = sum(1 for s in st if s["ok"])
    return "%d / %d 已完成" % (done, len(st)), done, len(st)


def needs_relaunch(boot_ax, boot_input_ok, ax_ok, input_ok):
    """本次会话里权限从「没有」变成「有」 —— macOS 要求重启进程才生效。

    这是"授权后还得重启"这句话的唯一判据: 对比进程启动那一刻的状态和现在的状态。
    """
    return (bool(ax_ok) and not boot_ax) or (bool(input_ok) and not boot_input_ok)


def perm_glyph(state):
    """步骤序号/状态那个小圆点。"""
    return {"done": "✓", "next": "●", "wait": "○"}.get(state, "○")


# ---------------------------------------------------------------- 控件小工具

def _color(name, fallback=None):
    try:
        return getattr(NSColor, name)()
    except Exception:                       # pragma: no cover
        return fallback if fallback is not None else NSColor.labelColor()


def _label(text, rect, size=13.0, bold=False, dim=False, right=False, color=None):
    f = NSTextField.alloc().initWithFrame_(rect)
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if right:
        f.setAlignment_(2)                  # NSTextAlignmentRight
    if color is not None:
        f.setTextColor_(color)
    elif dim:
        f.setTextColor_(_color("secondaryLabelColor"))
    return f


def _button(text, rect, target, action, switch=False, state=0, small=False):
    b = _FirstMouseButton.alloc().initWithFrame_(rect)
    b.setTitle_(text)
    if switch:
        b.setButtonType_(_BTN_SWITCH)
        b.setState_(1 if state else 0)
    else:
        b.setBezelStyle_(1)                 # NSBezelStyleRounded
    b.setFont_(NSFont.systemFontOfSize_(11.0 if small else 13.0))
    b.setTarget_(target)
    b.setAction_(action)
    return b


def _popup(rect, items):
    p = _FirstMousePopup.alloc().initWithFrame_pullsDown_(rect, False)
    p.addItemsWithTitles_(list(items))
    p.setFont_(NSFont.systemFontOfSize_(13.0))
    return p


# ---------------------------------------------------------------- 布局质检

def measure(text, font):
    a = NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: font})
    return float(a.size()[0])


def _rect(v):
    f = v.frame()
    return (float(f.origin.x), float(f.origin.y), float(f.size.width), float(f.size.height))


def _ident(v):
    """控件的 identifier (背景卡片 = "bg")。拿不到就给空串。"""
    try:
        return v.identifier() or ""
    except Exception:
        return ""


def _hit(a, b, slack=0.5):
    """两个矩形是否真重叠 —— 只挨着不算(布局里行与行本来就是贴着的)。"""
    return (a[0] + a[2] - slack > b[0] and b[0] + b[2] - slack > a[0]
            and a[1] + a[3] - slack > b[1] and b[1] + b[3] - slack > a[1])


def check_layout(view, w=None, h=None):
    """离线 GUI 质检: 控件越界 / 互相重叠 / 文字被切。

    我看不到屏幕, 所以"排得对不对"只能这样验: 每个控件的 frame 与它自己那句
    文字用 AppKit 量出来的宽度比。返回问题列表(空 = 干净)。

    w/h 不给就按**窗口当前内容尺寸**判 —— 主窗口现在会随状态变高变矮 (见 _relayout),
    拿常量判会把"本来就应该更高"的展开态误报成越界。
    """
    if w is None or h is None:
        try:
            f = view.frame()
            _sz = getattr(f, "size", None) or f[1]      # PyObjC 的 NSRect 是 ((x,y),(w,h))
            _w = float(getattr(_sz, "width", _sz[0]))
            _h = float(getattr(_sz, "height", _sz[1]))
        except Exception:
            _w, _h = W, H
        w = _w if w is None else w
        h = _h if h is None else h
    items = []
    for v in view.subviews():
        try:
            if v.identifier() in ("bg", "line"):
                continue        # 分组底本来就垫在控件底下, 不算"重叠"
        except Exception:
            pass
        txt = ""
        # 按钮 (含 _FirstMouseButton 这类子类) 的 stringValue 是"选中态"("0"/"1"),
        # 报告里要的是那句标题; 下拉框是按钮的子类, 但它走下面 titleOfSelectedItem 那条路。
        if isinstance(v, NSButton) and not isinstance(v, NSPopUpButton):
            try:
                txt = v.title() or ""
            except Exception:
                txt = ""
        if not txt:
            try:
                txt = v.stringValue() or ""
            except Exception:
                pass
        if not txt:
            try:
                txt = v.titleOfSelectedItem() or ""
            except Exception:
                pass
        items.append((v, _rect(v), txt))
    bad = []
    for v, r, txt in items:
        if r[0] < -0.5 or r[1] < -0.5 or r[0] + r[2] > w + 0.5 or r[1] + r[3] > h + 0.5:
            bad.append("越界: %r frame=%s (窗口 %gx%g)" % (txt[:18], tuple(r), w, h))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _hit(items[i][1], items[j][1]):
                bad.append("重叠: %r%s <-> %r%s" % (items[i][2][:16], tuple(items[i][1]),
                                                   items[j][2][:16], tuple(items[j][1])))
    # ★画到框外: NSView 默认不裁剪绘制, 视图可以画到自己的 frame 之外压住别人,
    #   而 frame 之间并不重叠 —— 审计看不见。自绘视图声明 content_box() 就按它验。
    for _v in view.subviews():
        _cb = getattr(_v, "content_box", None)
        if not callable(_cb):
            continue
        try:
            _b = _v.bounds().size
            _x0, _y0, _bw, _bh = _cb()
            if _x0 < -0.5 or _y0 < -0.5 or _x0 + _bw > _b.width + 0.5 \
                    or _y0 + _bh > _b.height + 0.5:
                bad.append("画到框外: %r 实际占用 (%.0f,%.0f,%.0f×%.0f) 超出视图 %.0f×%.0f"
                           % (tuple(_rect(_v)), _x0, _y0, _bw, _bh, _b.width, _b.height))
        except Exception:
            pass
    # ★组合控件内部审计看不见 —— NSButton 的默认标题是 "Button", 忘了清就会自己画一行字
    #   叠在图标上 (用户实测报过: "侧边栏有 button 字样重叠在上面")。这里显式钉住。
    for _v in view.subviews():
        if isinstance(_v, NSButton) and not isinstance(_v, NSPopUpButton):
            try:
                if _v.title() == "Button":
                    bad.append("按钮没清默认标题: %r (会画一行 'Button' 叠在图标上)"
                               % (tuple(_rect(_v)),))
            except Exception:
                pass
    for v, r, txt in items:
        if not txt:
            continue
        try:
            need = measure(txt, v.font())
        except Exception:
            continue
        if need > r[2] + 1.0:
            bad.append("文字放不下: %r 需要 %.0fpt, frame 只有 %.0fpt"
                       % (txt[:24], need, r[2]))
    # 「贴到卡片顶边」也看不出来 —— 用户报过的那次就是: 「恢复默认」按钮底边比卡片
    # 顶边还低 1pt, 不算重叠(重叠那条判据要两边都压过 slack, 1pt 只压了一边, 而且
    # 卡片自己在建 items 时就被跳过了), 但看着就是和下面的框粘在一起。留最小呼吸间距。
    # 注意坐标系: _r() 转出来的 frame 是 AppKit 的 y 向上, 卡片「顶边」= y + h。
    MIN_GAP = 3.5
    _cards = [_rect(_v) for _v in view.subviews() if _ident(_v) == "bg"]
    if os.environ.get("DPTOUCH_AUDIT_DEBUG"):
        print("     bg 视图 %d 个: %s" % (len(_cards), [_cards[i] for i in range(len(_cards))]))
    for v, r, txt in items:
        for _c in _cards:
            _ct = _c[1] + _c[3]                     # 卡片顶边
            _hx = min(r[0] + r[2], _c[0] + _c[2]) - max(r[0], _c[0])
            if _hx <= 0:
                continue                            # 水平上不在卡片上方, 不算
            if r[1] < _ct + MIN_GAP and r[1] + r[3] > _ct:
                bad.append("贴到卡片顶边: %r frame=%s 底边离卡片顶边(%.1f)只有 %.1fpt (要 >= %gpt)"
                           % (txt[:18], tuple(r), _ct, r[1] - _ct, MIN_GAP))
    # 控件的 action 名字写错(例如 setAction_("onMode_") —— 少了冒号)会静默失效:
    # 界面看着完全正常, 点了就是没反应。只能靠这条查出来。
    for v, r, txt in items:
        try:
            act = v.action()
            tgt = v.target()
        except Exception:
            continue
        if not act or tgt is None:
            continue
        try:
            ok = bool(tgt.respondsToSelector_(act))
        except Exception:
            continue
        if not ok:
            bad.append("action 无人响应: %r -> %r" % (txt[:18], act))
    return bad


def logo_path():
    """logo 图片路径: 先找 .app bundle 里的 Resources/logo.png, 再找源码树。

    装成 .app 之后源码目录是不存在的, 所以要两套都试; 找不到就干脆不画 logo,
    不能让"没有图标"把整个设置窗口拖崩。
    """
    cands = []
    try:
        b = NSBundle.mainBundle()
        for nm, ext in (("logo", "png"), ("logo", None)):
            p = b.pathForResource_ofType_(nm, ext) if b else None
            if p:
                cands.append(str(p))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (("docs", "logo.png"), ("..", "docs", "logo.png"),
                ("logo.png",), ("..", "logo.png")):
        cands.append(os.path.normpath(os.path.join(here, *rel)))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


LOGO_PX = 56.0                              # 窗口右上角 logo 的边长


def logo_view(path):
    iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, LOGO_PX, LOGO_PX))
    if path:
        try:
            img = NSImage.alloc().initWithContentsOfFile_(path)
            if img is not None and img.isValid():
                iv.setImage_(img)
                iv.setImageScaling_(3)      # 等比缩放填满 (NSImageScaleProportionallyUpOrDown)
        except Exception:
            pass
    iv.setEditable_(False)
    return iv          # 注意: NSImageView 没有 setBordered_ (那是 NSButton 的) —— 别顺手加回来


def dump_layout(view):
    for v in view.subviews():
        txt = ""
        if isinstance(v, NSButton) and not isinstance(v, NSPopUpButton):
            # NSButton 的 stringValue 是"选中态"不是标题, 这里要的是人看得懂的那句
            try:
                txt = "%s %s" % ("v" if v.state() else " ", v.title())
            except Exception:
                txt = ""
        if not txt:
            try:
                txt = v.stringValue() or ""
            except Exception:
                pass
        if not txt:
            try:
                txt = v.titleOfSelectedItem() or ""
            except Exception:
                pass
        try:                                # 下拉框: 把候选项也打出来 (布局检查看不到内容)
            _items = v.itemArray()
        except Exception:
            _items = None
        if _items:
            txt = "%s [%s]" % (txt, " | ".join(str(it.title()) for it in _items))
        try:
            if v.isHidden():
                txt = txt + "   [隐藏]"
        except Exception:
            pass
        r = _rect(v)
        print("  %-7.0f,%-6.0f %-5.0fx%-5.0f %-26s %s"
              % (r[0], r[1], r[2], r[3], v.__class__.__name__, txt[:46]), flush=True)


# ---------------------------------------------------------------- 窗口

class _Pane(NSObject):
    """两个窗口共用的手算坐标小工具 (子类设好 self._W / self._H 即可)。

    坐标一律按「从上往下」写, 这里翻成 AppKit 的从左下角 —— 布局代码读起来就是
    视觉顺序, 不用在脑子里做减法。
    """

    @objc.python_method
    def _r(self, x, y_top, w, h):
        return NSMakeRect(x, self._H - y_top - h, w, h)

    @objc.python_method
    def _card(self, v, y_top, h, x=None, w=None):
        """垫在控件底下的一块圆角分组底。要先加, 才在控件下面。"""
        x = M if x is None else x
        w = (self._W - 2 * M) if w is None else w
        b = _CardView.alloc().initWithFrame_(self._r(x, y_top, w, h))
        b.setIdentifier_("bg")
        v.addSubview_(b)
        return b

    @objc.python_method
    def _perms_done(self):
        """两项权限都齐了没 —— 齐了权限区就压成一行 (没齐才展开分步引导)。

        优先用「最后一次刷新看到的那份状态」: 无头自检会喂假状态进来, 排版和刷新
        必须看同一份, 不然两边判断不一致会来回重排。
        """
        st = self._last_st
        if st is None:
            try:
                st = self.app.engine.status()
            except Exception:
                return False
        try:
            return all(s["ok"] for s in perm_steps(
                bool(st.get("ax")),
                bool(st.get("running")) and int(st.get("rc", 0)) == 0))
        except Exception:
            return False

    @objc.python_method
    def _counter_row(self, v, y_top, x=None):
        """计数行: 默认收起, 点右上「详情」才展开。"""
        x = (M + PAD) if x is None else x
        self.c["counters"] = _label("", self._r(x, y_top, CR - x, 16),
                                    size=11, dim=True)
        v.addSubview_(self.c["counters"])
        return self.c["counters"]

    @objc.python_method
    def _select(self, popup, keys, value):
        """把下拉拉到 value 那一项。数字档位对不上时退到最接近的一档。"""
        try:
            i = keys.index(value)
        except ValueError:
            try:
                i = min(range(len(keys)), key=lambda k: abs(float(keys[k]) - float(value)))
            except Exception:
                i = 0
        popup.selectItemAtIndex_(i)


class SettingsWindow(_Pane):
    """设置窗口。常驻一个实例, 反复开关只做 orderFront_ / orderOut_。"""

    def initWithApp_appName_version_(self, app, name, version):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self.app = app
        self.name = name or ""
        self.version = version or ""
        self._W, self._H = W, H              # 手算坐标用 (见 _Pane)
        self.c = {}                          # 控件登记表
        self._syncing = False
        self._n = 0
        self._focus_left = 0            # 窗口刚起来时还欠几次"抢焦点"
        self._disp_sig = None
        self._details = False
        self.page = "overview"          # 侧栏当前页 (overview/gestures/display/runtime)
        self._disp_wh = None            # 平板屏当前档位 (由 _sync_display 缓存, 侧栏/概览读它)
        self._disp_name = ""           # 「详情」开关: 计数行默认收起
        self._laid_perm_done = None     # 上次排版时权限齐没齐 (变了要整页重排)
        self._last_st = None            # 最后一次刷新看到的状态 (排版也用这份)
        self._build()
        return self

    # ---------------- 坐标: 见 _Pane ----------------
    @objc.python_method
    def _sec(self, v, text, y_top, note=None):
        """一行分组标题 (12pt semibold 次级色), 返回它下面第一行的起始 y。

        golden-gate: 分组靠「标题 + 间距」, 不再靠一层层圆角卡片 (那是网页仪表盘的做法)。
        """
        v.addSubview_(_label(text, self._r(M, y_top, 240, 16), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        if note:
            v.addSubview_(_label(note, self._r(CR - 300, y_top, 300, 16),
                                 size=11, dim=True, right=True))
        return y_top + 24        # 标题 16 + 与内容 8pt (heyimjames 4pt 栅格)

    # ---------------- 构建 ----------------
    @objc.python_method
    def _build(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        self.win.setTitle_(self.name)
        self.win.setReleasedWhenClosed_(False)   # 关掉不销毁: 下次还能开
        self.win.center()
        self._layout()
        # 高级选项窗口: 独立小窗。内容全在主窗口这类里算 frame 会牵动整页重排,
        # 而且「高级」本来就该离常用开关远一点。
        self.adv = AdvancedWindow.alloc().initWithOwner_(self)

    @objc.python_method
    def _relayout(self, animate=True):
        """按当前状态重建内容并调整窗口高度。

        清空 contentView 重建, 再按实际用到的尺寸改窗口高度 —— 顶边留在原处,
        不然改高度窗口会往上长 (用户会觉得"跳了一下")。
        高度真变了就做 0.22s 的动画 + 内容淡入 (减弱动态效果开着时直接切)。
        """
        v = self.win.contentView()
        for sub in list(v.subviews()):
            try:
                sub.removeFromSuperview()
            except Exception:
                pass
        self.c = {}
        self._layout()
        try:
            f = self.win.frame()
            old_h = self.win.contentRectForFrameRect_(f).size.height
            new_h = self._H
            # 顶边不动: 高度差补回原点的 y
            target = NSMakeRect(f.origin.x, f.origin.y + (old_h - new_h),
                                f.size.width, f.size.height + (new_h - old_h))
            if animate and abs(new_h - old_h) > 0.5 and not reduce_motion():
                from AppKit import NSAnimationContext
                v.setWantsLayer_(True)
                v.setAlphaValue_(0.0)        # 先隐形, 免得看见文字在跳
                NSAnimationContext.beginGrouping()
                ctx = NSAnimationContext.currentContext()
                ctx.setDuration_(0.22)
                try:
                    from Quartz import CAMediaTimingFunction
                    ctx.setTimingFunction_(
                        CAMediaTimingFunction.functionWithName_("easeOut"))
                except Exception:
                    pass
                ctx.setCompletionHandler_(lambda: v.setAlphaValue_(1.0))
                self.win.animator().setFrame_display_(target, True)
                v.animator().setAlphaValue_(1.0)
                NSAnimationContext.endGrouping()
            else:
                self.win.setFrame_display_(target, True)
                v.setAlphaValue_(1.0)
        except Exception as e:
            try:
                self.app.log("重排后改窗口高度失败: %r" % (e,))
            except Exception:
                pass
        # ★重排完接着用**同一份状态**刷新: 这里若不带 st, 刷新会再去问一次引擎, 而引擎的
        #   真实状态可能和刚喂进来的不一致 (无头自检喂假状态时必现) —— 于是"权限完成态变了"
        #   再次成立, 又翻回上一版, 来回重排 (实测: 权限就绪那一版永远排不出来)。
        self.refresh(self._last_st or None, force=True)

    @objc.python_method
    def _layout(self):
        """外壳 + 当前页。侧栏常驻, 只有激活的那一页进视图树。

        ★只加激活页: 四页叠在一起的话布局自检会把它们判成互相重叠 (隐藏视图也有 frame)。
        切页 = 整窗重建 (_relayout), 和权限态切换走同一条路。
        """
        v = self.win.contentView()
        self._H = H
        cx = M + PAD
        done = self._perms_done()
        self._laid_perm_done = done

        y = 0.0

        # ---- 主体: 侧栏 + 内容区 ----
        body_h = H - TOPBAR_H - 1 - STRIP_H
        self._sidebar(v, y, body_h)
        v.addSubview_(_fill_view(self._r(SIDEBAR_W, y, 1, body_h), "separatorColor",
                                 ident="line"))
        v.addSubview_(_fill_view(self._r(SIDEBAR_W + 1, y, W - SIDEBAR_W - 1, body_h),
                                 "controlBackgroundColor", ident="line"))
        self._page(v, y, body_h)
        y += body_h

        # ---- 底部状态条 ----
        v.addSubview_(_line_view(self._r(0, y, W, 1)))
        y += 1
        self.c["strip_dot"] = _label("●", self._r(M, y + 13, 14, 16), size=11,
                                     color=_color("systemGreenColor"))
        v.addSubview_(self.c["strip_dot"])
        self.c["strip"] = _label("设备在线 · 配置已就绪", self._r(M + 17, y + 12, 420, 16),
                                 size=11, color=_color("secondaryLabelColor"))
        v.addSubview_(self.c["strip"])
        v.addSubview_(_label("使用说明 · ⌘1–4 切换页面",
                             self._r(W - M - 260, y + 12, 260, 16), size=11, dim=True, right=True))
        used = y + STRIP_H

        # 自检: 摆出来的高度要和开头算的一致 (差了就是某段的行高/内边距改了没同步)
        if abs(used - self._H) > 1:
            try:
                self.app.log("窗口高度算错: 摆出来 %g, 预期 %g" % (used, self._H))
            except Exception:
                pass
        return True

    # ---------------- 外壳: 侧栏 / 页面 ----------------
    @objc.python_method
    def _sidebar(self, v, y0, h):
        """侧栏: 品牌 + 导航 + 设备区 + 底部条 (设计稿 .sidebar)。

        ★导航项里的图标/文字/徽标都放在**按钮内部** —— 布局自检只走一层, 放外面会被
        判成"和按钮重叠"。
        """
        v.addSubview_(_fill_view(self._r(0, y0, SIDEBAR_W, h), "windowBackgroundColor",
                                 ident="line"))
        y = y0 + 20
        lg = logo_view(logo_path())
        if lg is not None:
            lg.setFrame_(self._r(19, y, 54, 54))
            v.addSubview_(lg)
        # 品牌: 应用名在 logo 右侧（连接状态不再重复显示 —— 底部状态条已有）
        _lb = _label(self.name, self._r(19 + 54 + 12, y + 16, SIDEBAR_W - 85 - 14, 22),
                     size=14, bold=True)
        from AppKit import NSLineBreakByTruncatingTail
        _lb.setUsesSingleLineMode_(True)                     # ★标题不分两行
        _lb.setLineBreakMode_(NSLineBreakByTruncatingTail)   # 放不下就省略号, 绝不换行
        v.addSubview_(_lb)
        y += 54 + 15
        y += 4
        for key, icon, title in PAGES:
            sel = (key == self.page)
            btn = _NavButton.alloc().initWithFrame_(self._r(11, y, SIDEBAR_W - 22, NAV_H))
            btn.key = key
            btn.setBordered_(False)
            btn.setTitle_("")            # ★不清掉的话 NSButton 会画默认的 "Button" 叠在图标上
            btn.setState_(1 if sel else 0)
            btn.setTarget_(self)
            btn.setAction_("onNav:")
            ic = _icon(icon, NAV_ICON, 0.0,
                       brand_color() if sel else _color("secondaryLabelColor"))
            if ic is not None:
                ic.setFrame_(NSMakeRect(13, (NAV_H - NAV_ICON) / 2.0, NAV_ICON, NAV_ICON))
                btn.addSubview_(ic)
            lb = _label(title, NSMakeRect(41, (NAV_H - 18) / 2.0, 120, 18), size=13, bold=sel)
            if sel:
                lb.setTextColor_(_color("labelColor"))
            btn.addSubview_(lb)
            v.addSubview_(btn)
            self.c["nav_%s" % key] = btn
            y += NAV_H + 2

        # ---- 设备区 (贴底): 名称 + 三行实时小项 ----
        mini = 3 * 18.0
        art_h = 126.0                      # 设备图: 够放下旋转后的平板外接矩形 (自检会断言)
        area = 16 + 6 + art_h + 6 + 18 + 4 + 16 + 12 + 1 + 13 + mini + 12
        dy = y0 + h - 30 - area
        v.addSubview_(_label("当前设备", self._r(21, dy, 86, 14), size=11, bold=True,
                             color=_color("secondaryLabelColor")))
        self.c["live_tag"] = _label("● 已连接", self._r(SIDEBAR_W - 21 - 80, dy, 80, 14),
                                    size=11, color=_color("systemGreenColor"), right=True)
        v.addSubview_(self.c["live_tag"])
        dy += 16 + 6
        self.c["dev_art"] = _DeviceArt.alloc().initWithFrame_(
            self._r(0, dy, SIDEBAR_W, art_h))
        self.c["dev_art"].setIdentifier_("line")      # 装饰件: 审计跳过
        v.addSubview_(self.c["dev_art"])
        dy += art_h + 6
        self.c["dev_name"] = _label("未连接平板", self._r(21, dy, SIDEBAR_W - 42, 18), size=13,
                                    bold=True)
        v.addSubview_(self.c["dev_name"])
        dy += 18 + 4
        self.c["dev_meta"] = _label("", self._r(21, dy, SIDEBAR_W - 42, 16), size=11, dim=True)
        v.addSubview_(self.c["dev_meta"])
        dy += 16 + 8
        v.addSubview_(_line_view(self._r(21, dy, SIDEBAR_W - 42, 1)))
        dy += 13
        for i, (lab, key) in enumerate((("显示连接", "mini_disp"), ("触控接口", "mini_touch"),
                                        ("输入事件", "mini_events"))):
            v.addSubview_(_label(lab, self._r(21, dy + i * 18, 52, 16), size=11, dim=True))
            self.c[key] = _label("", self._r(SIDEBAR_W - 21 - 120, dy + i * 18, 120, 16),
                                 size=11, right=True)
            v.addSubview_(self.c[key])

        # ---- 侧栏底条 ----
        by = y0 + h - 30
        v.addSubview_(_line_view(self._r(0, by, SIDEBAR_W, 1)))
        self.c["ver"] = _label("", self._r(21, by + 9, 88, 16), size=11, dim=True)
        v.addSubview_(self.c["ver"])
        v.addSubview_(_label("MIT License", self._r(SIDEBAR_W - 21 - 86, by + 9, 86, 16),
                             size=11, dim=True, right=True))

    @objc.python_method
    def _page(self, v, y0, h):
        """内容区: 只画当前页 (设计稿的 .content, 最大宽 810 居中)。"""
        pane_x, pane_w = SIDEBAR_W + 1, W - SIDEBAR_W - 1
        pw = min(CONTENT_MAX, pane_w - 92.0)          # 左右各留 46 (设计稿 .content)
        px = pane_x + (pane_w - pw) / 2.0
        y = y0 + 26
        if self.page == "overview":
            y = self._page_overview(v, px, pw, y)
        elif self.page == "gestures":
            y = self._page_gestures(v, px, pw, y)
        elif self.page == "display":
            y = self._page_display(v, px, pw, y)
        else:
            y = self._page_runtime(v, px, pw, y)

    @objc.python_method
    def _eyebrow(self, v, x, y, text):
        v.addSubview_(_label(text, self._r(x, y, 400, 14), size=10, bold=True,
                             color=brand_color()))
        return y + 14 + 10

    @objc.python_method
    def _page_head(self, v, x, w, y, title, subtitle, btn_title=None, btn_act=None):
        v.addSubview_(_label(title, self._r(x, y, w - 200, 36), size=28, bold=True))
        v.addSubview_(_label(subtitle, self._r(x, y + 40, w - 200, 18), size=12,
                             color=_color("secondaryLabelColor")))
        if btn_title:
            self.c["head_btn"] = _button(btn_title, self._r(x + w - 108, y + 6, 108, 26),
                                         self, btn_act, small=True)
            v.addSubview_(self.c["head_btn"])
        return y + 40 + 18 + 14

    @objc.python_method
    def _page_overview(self, v, x, w, y):
        y = self._eyebrow(v, x, y, "DEVICE OVERVIEW / 00")
        # 右上角: 三个快捷按钮（🌙 外观 / ↻ 重新检测 / ⓘ 设备详情）
        for i, (icon, tip, act) in enumerate((("moon", "切换浅色 / 深色外观", "onTheme:"),
                                              ("arrow.clockwise", "重新检测设备", "onRecheck:"),
                                              ("info.circle", "设备详情", "onDetails:"))):
            btn = _IconButton.alloc().initWithFrame_(
                self._r(x + w - 32 - i * 36, y - 30, 32, 30))
            btn.setBordered_(False)
            btn.setTitle_("")
            btn.setToolTip_(tip)
            btn.setTarget_(self)
            btn.setAction_(act)
            ic = _icon(icon, 16.0)
            if ic is not None:
                ic.setFrame_(NSMakeRect(8, 7, 16, 16))
                btn.addSubview_(ic)
            v.addSubview_(btn)
            self.c["tb_%s" % icon.replace(".", "_")] = btn
        done = self._perms_done()
        # hero: 大标题 + 检测结果 + 一句说明
        v.addSubview_(_label("连接完成，随时开始。" if done else "还差一步就能用。",
                             self._r(x, y, w - 40, 40), size=30, bold=True))
        y += 44
        self.c["perm_ok"] = _label("✓" if done else "!", self._r(x, y + 1, 18, 18), size=13,
                                   bold=True, color=(_color("systemGreenColor") if done
                                                     else _color("systemOrangeColor")))
        v.addSubview_(self.c["perm_ok"])
        self.c["perm_sum"] = _label("", self._r(x + 22, y, 400, 20), size=13)
        v.addSubview_(self.c["perm_sum"])
        y += 18 + 10
        v.addSubview_(_label("小米平板 9 Pro Max 作为触控输入设备接入这台 Mac。手势映射与显示配置"
                             "都在左侧页面里。", self._r(x, y, w - 20, 40), size=13,
                             color=_color("secondaryLabelColor")))
        y += 40 + 16
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 18
        # 未完成: 分步引导 (唯一保留的"框" —— 会消失、自带动作的临时块)
        if not done:
            self._perm_block(v, x, y, w)
            y += PERM_H_FULL + 18
        # 当前手势配置
        v.addSubview_(_label("当前手势配置", self._r(x, y, 300, 16), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        y += 16 + 10
        binds = HB.binds_from_cfg(self.app.cfg)
        for i, (g, title, _o, _d) in enumerate(BIND_ROWS):
            col = i % 2
            row = i // 2
            bx = x + col * (w / 2.0)
            by = y + row * 46
            v.addSubview_(_label(title, self._r(bx, by, 120, 18), size=14, dim=True))
            self.c["sum_%s" % g] = _label(HB.action_title(g, binds.get(g)),
                                          self._r(bx + 130, by, 200, 18), size=14, bold=True)
            v.addSubview_(self.c["sum_%s" % g])
        y += 3 * 46 + 6
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 18
        # 显示器 / 手指触控 两栏
        v.addSubview_(_label("显示器", self._r(x, y, 200, 16), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        v.addSubview_(_label("手指触控", self._r(x + w / 2.0, y, 200, 16), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        y += 16 + 8
        self.c["ov_disp"] = _label("", self._r(x, y, w / 2.0 - 30, 18), size=13, bold=True)
        v.addSubview_(self.c["ov_disp"])
        self.c["enhanced_hint"] = _label("", self._r(x + w / 2.0, y, w / 2.0 - 30, 18),
                                         size=13, bold=True)
        v.addSubview_(self.c["enhanced_hint"])
        y += 18 + 6
        self.c["ov_disp_sub"] = _label("", self._r(x, y, w / 2.0 - 30, 16), size=11, dim=True)
        v.addSubview_(self.c["ov_disp_sub"])
        v.addSubview_(_label("轻点、拖拽直接用手指，不用笔", self._r(x + w / 2.0, y,
                                                                    w / 2.0 - 30, 16),
                             size=11, dim=True))
        return y + 16

    @objc.python_method
    def _perm_block(self, v, x, y, w):
        """分步授权块 (权限没齐时才画)。"""
        self._card(v, y, PERM_H_FULL, x=x, w=w)
        for i, key in ((0, "ax"), (1, "input")):
            ry = y + PAD + i * 44
            self.c["p_%s_dot" % key] = _label("1", self._r(x + PAD, ry + 12, 20, 16),
                                              size=12, bold=True)
            v.addSubview_(self.c["p_%s_dot" % key])
            self.c["p_%s_title" % key] = _label("", self._r(x + PAD + 22, ry + 6, 250, 16),
                                                size=13, bold=True)
            v.addSubview_(self.c["p_%s_title" % key])
            self.c["p_%s_why" % key] = _label("", self._r(x + PAD + 22, ry + 26, 420, 15),
                                              size=11, dim=True)
            v.addSubview_(self.c["p_%s_why" % key])
            self.c["p_%s_btn" % key] = _button("打开设置",
                                               self._r(x + w - PAD - 104, ry + 12, 104, 26),
                                               self, "onPerm:")
            self.c["p_%s_btn" % key].setTag_(i)
            v.addSubview_(self.c["p_%s_btn" % key])
        self.c["hint"] = _label("", self._r(x + PAD, y + PAD + 92, w - 2 * PAD, 16),
                                size=11, dim=True)
        v.addSubview_(self.c["hint"])

    @objc.python_method
    def _page_gestures(self, v, x, w, y):
        y = self._eyebrow(v, x, y, "INPUT SETTINGS / 01")
        y = self._page_head(v, x, w, y, "手势映射", "为轻点、滑动和长按设置对应的鼠标操作。",
                            "恢复默认", "onResetBinds:")
        v.addSubview_(_label("● 当前配置 · 小米平板 9 Pro Max      自动保存",
                             self._r(x, y, w, 16), size=11, color=_color("systemGreenColor")))
        y += 16 + 12
        v.addSubview_(_label("触控动作", self._r(x, y, 200, 16), size=11, bold=True,
                             color=_color("secondaryLabelColor")))
        v.addSubview_(_label("映射行为", self._r(x + w - 200, y, 200, 16), size=11, bold=True,
                             color=_color("secondaryLabelColor"), right=True))
        y += 16 + 8
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 1
        notes = {g: d for g, _t, _o, d in HB.BIND_GESTURES}
        ROWG = 48.0                       # 定高窗口放不下 56 的行距, 收到 48
        for i, (g, title, _o, _d) in enumerate(BIND_ROWS):
            ry = y + i * ROWG
            ic = _icon({"tap": "hand.tap", "swipe": "hand.draw", "hold_swipe": "hand.point.up.left",
                        "hold": "cursorarrow.click"}.get(g, "hand.point.up"), 20.0)
            if ic is not None:
                ic.setFrame_(self._r(x, ry + 13, 20, 20))
                v.addSubview_(ic)
            v.addSubview_(_label(title, self._r(x + 32, ry + 8, 200, 18), size=13, bold=True))
            v.addSubview_(_label(notes.get(g, ""), self._r(x + 32, ry + 27, 300, 15),
                                 size=11, dim=True))
            pop = _popup(self._r(x + w - 252, ry + 10, 252, 26), HB.gesture_action_titles(g))
            pop.setTarget_(self)
            pop.setAction_("onBind:")
            pop.setTag_(i)
            v.addSubview_(pop)
            self.c["bind_%s" % g] = pop
            v.addSubview_(_line_view(self._r(x, ry + ROWG - 1, w, 1)))
        y += 4 * ROWG
        # 滚动方向 / 速度 (并一行)
        v.addSubview_(_label("滚动", self._r(x, y + 8, 200, 18), size=13, bold=True))
        v.addSubview_(_label("滚动方向与速度", self._r(x + 32, y + 27, 300, 15), size=11, dim=True))
        self.c["nat"] = _popup(self._r(x + w - 252, y + 10, 130, 26),
                               [t for _v, t in NAT_LABELS])
        self.c["nat"].setTarget_(self)
        self.c["nat"].setAction_("onNat:")
        v.addSubview_(self.c["nat"])
        self.c["gain"] = _popup(self._r(x + w - 114, y + 10, 114, 26), self._gain_titles())
        self.c["gain"].setTarget_(self)
        self.c["gain"].setAction_("onGain:")
        v.addSubview_(self.c["gain"])
        v.addSubview_(_line_view(self._r(x, y + ROWG - 1, w, 1)))
        y += ROWG
        # 增强模式: 整行 (开关在右)
        v.addSubview_(_label("增强模式 · 手指触控", self._r(x, y + 8, 300, 18), size=13,
                             bold=True))
        v.addSubview_(_label("检测到触摸屏接口，可直接使用手指点击与拖拽",
                             self._r(x, y + 27, 460, 15), size=11, dim=True))
        self.c["enhanced"] = _Switch.alloc().initWithFrame_(self._r(x + w - 36, y + 13, 36, 22))
        self.c["enhanced"].setBordered_(False)
        self.c["enhanced"].setTitle_("")
        self.c["enhanced"].setTarget_(self)
        self.c["enhanced"].setAction_("onEnhanced:")
        v.addSubview_(self.c["enhanced"])
        v.addSubview_(_line_view(self._r(x, y + ROWG - 1, w, 1)))
        y += ROWG + 16
        return y

    @objc.python_method
    def _page_display(self, v, x, w, y):
        # 进这一页先同步一次档位缓存(31ms), 免得下拉停在「正在扫描…」等下一次 tick
        try:
            self.app._sync_display(force=True)
        except Exception:
            pass
        y = self._eyebrow(v, x, y, "DISPLAY SETTINGS / 02")
        y = self._page_head(v, x, w, y, "显示与缩放",
                            "选择屏幕输出模式，保持清晰度与触控坐标一致。",
                            "重新扫描", "onDispRescan:")
        v.addSubview_(_label("连接显示器", self._r(x, y, 200, 16), size=11, bold=True,
                             color=_color("secondaryLabelColor")))
        y += 16 + 10
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 1
        # 当前显示器
        v.addSubview_(_label("当前显示器", self._r(x, y + 16, 200, 18), size=13, bold=True))
        self.c["disp_dev"] = _label("", self._r(x, y + 36, 300, 15), size=11, dim=True)
        v.addSubview_(self.c["disp_dev"])
        v.addSubview_(_label("● 已连接", self._r(x + w - 120, y + 17, 120, 16), size=11,
                             color=_color("systemGreenColor"), right=True))
        v.addSubview_(_line_view(self._r(x, y + 60, w, 1)))
        y += 60
        # 输出分辨率 (真控件: 缩放档位下拉)
        v.addSubview_(_label("输出分辨率", self._r(x, y + 16, 200, 18), size=13, bold=True))
        v.addSubview_(_label("推荐保持像素 1:1 以获得最佳锐度", self._r(x, y + 36, 320, 15),
                             size=11, dim=True))
        self.c["disp_btn"] = _button("重新扫描", self._r(x + w - 108, y + 16, 108, 26),
                                     self, "onDispRescan:")
        v.addSubview_(self.c["disp_btn"])
        v.addSubview_(_line_view(self._r(x, y + 60, w, 1)))
        y += 60
        self.c["disp"] = _popup(self._r(x, y + 10, min(380.0, w - 30), 26), ["正在扫描…"])
        self.c["disp"].setTarget_(self)
        self.c["disp"].setAction_("onDisp:")
        v.addSubview_(self.c["disp"])
        self.c["disp_hint"] = _label("", self._r(x, y + 10, w, 16), size=11, dim=True)
        v.addSubview_(self.c["disp_hint"])
        return y + 30

    @objc.python_method
    def _page_runtime(self, v, x, w, y):
        y = self._eyebrow(v, x, y, "APP SETTINGS / 03")
        y = self._page_head(v, x, w, y, "运行与诊断", "后台行为、启动方式与问题排查。",
                            "高级选项", "onAdvanced:")
        v.addSubview_(_label("常规", self._r(x, y, 200, 16), size=11, bold=True,
                             color=_color("secondaryLabelColor")))
        y += 16 + 10
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 1
        for key, title, sub, act in (
                ("enable", "笔桥接", "将触控笔输入转换为 Mac 鼠标行为", "onEnable:"),
                ("autostart", "开机自启", "登录 macOS 后自动运行", "onAutostart:"),
                ("debug", "详细日志", "记录设备检测与输入映射的诊断信息", "onDebug:")):
            v.addSubview_(_label(title, self._r(x, y + 16, 300, 18), size=13, bold=True))
            v.addSubview_(_label(sub, self._r(x, y + 36, 420, 15), size=11, dim=True))
            self.c[key] = _Switch.alloc().initWithFrame_(self._r(x + w - 36, y + 20, 36, 22))
            self.c[key].setBordered_(False)
            self.c[key].setTitle_("")
            self.c[key].setTarget_(self)
            self.c[key].setAction_(act)
            v.addSubview_(self.c[key])
            v.addSubview_(_line_view(self._r(x, y + 60, w, 1)))
            y += 60
        y += 18
        v.addSubview_(_label("诊断", self._r(x, y, 200, 16), size=11, bold=True,
                             color=_color("secondaryLabelColor")))
        y += 16 + 10
        v.addSubview_(_line_view(self._r(x, y, w, 1)))
        y += 1
        v.addSubview_(_label("触控屏接口", self._r(x, y + 16, 300, 18), size=13, bold=True))
        self.c["diag_touch"] = _label("", self._r(x, y + 36, 420, 15), size=11, dim=True)
        v.addSubview_(self.c["diag_touch"])
        v.addSubview_(_line_view(self._r(x, y + 60, w, 1)))
        y += 60
        v.addSubview_(_label("高级诊断", self._r(x, y + 16, 300, 18), size=13, bold=True))
        self.c["adv_hint"] = _label("光标基准屏 · 判定时长 · 调试信息",
                                    self._r(x, y + 36, 420, 15), size=11, dim=True)
        v.addSubview_(self.c["adv_hint"])
        self.c["adv"] = _button("查看选项", self._r(x + w - 108, y + 16, 108, 26),
                                self, "onAdvanced:")
        v.addSubview_(self.c["adv"])
        v.addSubview_(_line_view(self._r(x, y + 60, w, 1)))
        y += 60 + 16
        v.addSubview_(_label("关于 Mipad DP Touch 1.2 · MIT License", self._r(x, y, w, 16),
                             size=11, dim=True))
        return y + 16

    # ---------------- 交互: 切页 / 外观 / 详情 / 提示条 ----------------
    def onNav_(self, sender):
        """侧栏切页 (⌘1–4 同效)。整窗重建 —— 和权限态切换走同一条路。"""
        key = getattr(sender, "key", "")
        if key and key != self.page:
            self.page = key
            self._relayout(animate=False)

    def onTheme_(self, sender):
        """浅色 / 深色外观切换 (设计稿右上角那颗月亮)。"""
        from AppKit import NSApp, NSAppearance
        try:
            dark = "Dark" in str(NSApp.effectiveAppearance().name())
            NSApp.setAppearance_(NSAppearance.appearanceNamed_(
                "NSAppearanceNameAqua" if dark else "NSAppearanceNameDarkAqua"))
            self.toast("已切换为%s外观" % ("浅色" if dark else "深色"))
        except Exception as e:
            self.app.log("切换外观失败: %r" % (e,))

    def onDetails_(self, sender):
        """设备详情 (设计稿右上角那颗 info): 用提示条报当前连接实况。"""
        st = self._last_st or {}
        devs = st.get("devices") or []
        if devs:
            self.toast("%s · 已连接 · 事件 %d" % (devs[0][0], st.get("events", 0)))
        else:
            self.toast("还没检测到平板 · 插上 USB-C，或看「设备概览」里的权限")

    def onToastExpire_(self, timer):
        tv = self.c.get("toast")
        if tv is None:
            return
        try:
            if not reduce_motion():
                from AppKit import NSAnimationContext
                NSAnimationContext.beginGrouping()
                NSAnimationContext.currentContext().setDuration_(0.2)
                tv.animator().setAlphaValue_(0.0)
                NSAnimationContext.endGrouping()
            tv.removeFromSuperview()
        except Exception:
            pass
        self.c["toast"] = None

    @objc.python_method
    def toast(self, msg, seconds=2.3):
        """临时提示条: 窗口底部中间, 2.3 秒后自己淡掉 (设计稿的 .toast)。"""
        try:
            old = self.c.get("toast")
            if old is not None:
                old.removeFromSuperview()
            w = max(150.0, measure(msg, NSFont.systemFontOfSize_(12.0)) + 36.0)
            tv = _ToastView.alloc().initWithFrame_(
                self._r((W - w) / 2.0, H - STRIP_H - 52.0, w, 34.0))
            tv.text = msg
            tv.setIdentifier_("bg")
            self.win.contentView().addSubview_(tv)
            self.c["toast"] = tv
            if not reduce_motion():
                from AppKit import NSAnimationContext
                tv.setWantsLayer_(True)
                tv.setAlphaValue_(0.0)
                NSAnimationContext.beginGrouping()
                NSAnimationContext.currentContext().setDuration_(0.16)
                tv.animator().setAlphaValue_(1.0)
                NSAnimationContext.endGrouping()
            from Foundation import NSTimer
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                seconds, self, "onToastExpire:", None, False)
        except Exception as e:
            self.app.log("提示条失败: %r" % (e,))

    @objc.python_method
    def _gain_titles(self):
        from dptouch_engine import GAINS
        return ["%.3gx %s" % (g, lab) for g, lab in GAINS]

    # ---------------- 显示 / 隐藏 ----------------
    @objc.python_method
    def show(self):
        if self.win is None:
            return False
        try:
            self._focus_left = 6            # 头几拍接着抢(菜单刚收起时激活会被忽略)
            self._activate()
            from AppKit import NSApp as _A
            self.app.log("设置窗口: 已请求激活 (App 激活=%s 窗口 key=%s)"
                         % (bool(_A.isActive()), bool(self.win.isKeyWindow())))
        except Exception as e:
            self.app.log("打开设置窗口失败: %r" % (e,))
            return False
        self.refresh(force=True)
        self._fade_in()
        return True

    @objc.python_method
    def _fade_in(self):
        """窗口出现时淡入 0.18s (减弱动态效果开着就直接显示)。"""
        if reduce_motion():
            return
        try:
            from AppKit import NSAnimationContext
            v = self.win.contentView()
            v.setWantsLayer_(True)
            v.setAlphaValue_(0.0)
            NSAnimationContext.beginGrouping()
            ctx = NSAnimationContext.currentContext()
            ctx.setDuration_(0.18)
            try:
                from Quartz import CAMediaTimingFunction
                ctx.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeOut"))
            except Exception:
                pass
            ctx.setCompletionHandler_(lambda: v.setAlphaValue_(1.0))
            v.animator().setAlphaValue_(1.0)
            NSAnimationContext.endGrouping()
        except Exception:
            pass

    @objc.python_method
    def _pulse_dot(self, on):
        """连接正常时状态圆点轻微呼吸 —— 只表示"活着", 不用变形/弹跳解释状态
        (golden-gate); 减弱动态效果开着就不动。"""
        dot = self.c.get("conn_dot")
        if dot is None:
            return
        try:
            dot.setWantsLayer_(True)
            lay = dot.layer()
            if not on or reduce_motion():
                if lay.animationForKey_("pulse") is not None:
                    lay.removeAnimationForKey_("pulse")
                dot.setAlphaValue_(1.0)
                return
            if lay.animationForKey_("pulse") is not None:
                return                      # 已经在跳了, 别每拍重启
            from Quartz import CABasicAnimation
            a = CABasicAnimation.animationWithKeyPath_("opacity")
            a.setFromValue_(1.0)
            a.setToValue_(0.45)
            a.setDuration_(0.9)
            a.setAutoreverses_(True)
            a.setRepeatCount_(1.0e9)
            lay.addAnimation_forKey_(a, "pulse")
        except Exception:
            pass

    @objc.python_method
    def _activate(self):
        """把窗口抬到最前并让它成为 key。

        窗口不是 key 的时候, 系统会把里面的下拉框画成灰的 —— 这正是"笔输入的选项
        都是灰色的"的由来; 顺带第一次点击也会被吃掉。三种 API 都试, 老系统没有
        activate() 就退回 activateIgnoringOtherApps_()。
        """
        from AppKit import NSApp, NSRunningApplication
        self.win.makeKeyAndOrderFront_(None)
        self.win.orderFrontRegardless()
        for fn in (lambda: NSApp.activate(),                    # macOS 14+ 的正路
                   lambda: NSApp.activateIgnoringOtherApps_(True),
                   lambda: NSRunningApplication.currentApplication()
                   .activateWithOptions_(2)):                   # 2 = IgnoringOtherApps
            try:
                fn()
            except Exception:
                pass

    @objc.python_method
    def toggle(self):
        if self.win is not None and self.win.isVisible():
            self._focus_left = 0
            self.win.orderOut_(None)
            try:
                self.adv.hide()          # 主窗口收起时高级选项跟着收起 (它是同一套设置)
            except Exception:
                pass
            return False
        return self.show()

    @objc.python_method
    def is_visible(self):
        return bool(self.win is not None and self.win.isVisible())

    # ---------------- 刷新(菜单与窗口共用的唯一渲染入口) ----------------
    @objc.python_method
    def refresh(self, st=None, force=False):
        if self.win is None:
            return
        # 高级选项是独立小窗: 它自己判可见性 (主窗口关着、它开着的时候也得刷)
        try:
            if self.adv is not None:
                self.adv.refresh()
        except Exception as e:
            self.app.log("高级选项窗口刷新失败: %r" % (e,))
        if not force and not self.win.isVisible():
            return
        try:
            self._refresh(st, force)
        except Exception as e:              # 定时器回调里出错不能静默
            self.app.log("设置窗口刷新失败: %r" % (e,))

    @objc.python_method
    def refresh_all(self):
        """主窗口 + 高级选项一起刷 (改高级里那几项, 主窗口的可用状态也跟着变)。"""
        self.refresh(force=True)
        try:
            if self.adv is not None:
                self.adv.refresh(force=True)
        except Exception as e:
            self.app.log("高级选项窗口刷新失败: %r" % (e,))

    @objc.python_method
    def _refresh(self, st, force):
        self._n += 1
        a = self.app
        st = st if st is not None else a.engine.status()
        get = (st or {}).get
        self._last_st = st or {}

        # --- 状态 ---
        devs = get("devices") or []
        names = "、".join(d[0] for d in devs)
        if names:
            self.c.get("conn") and self.c.get("conn") and self.c["conn"].setStringValue_("已连接 · %s" % names)
            self.c.get("conn_dot") and self.c.get("conn_dot") and self.c["conn_dot"].setTextColor_(_color("systemGreenColor"))
        elif get("running"):
            self.c.get("conn") and self.c.get("conn") and self.c["conn"].setStringValue_("已开启 · 等平板上线")
            self.c.get("conn_dot") and self.c.get("conn_dot") and self.c["conn_dot"].setTextColor_(_color("systemOrangeColor"))
        else:
            self.c.get("conn") and self.c.get("conn") and self.c["conn"].setStringValue_("未运行")
            self.c.get("conn_dot") and self.c.get("conn_dot") and self.c["conn_dot"].setTextColor_(_color("systemRedColor"))
        self._pulse_dot(bool(names))        # 连上了才呼吸; 其它状态保持静止
        if "counters" in self.c:            # 计数行只在右上「详情」展开时存在
            self.c["counters"].setStringValue_(
                "事件 %d · 点击 %d · 拖拽 %d · 滚动 %d"
                % (get("events", 0), get("click", 0), get("drag", 0), get("scroll", 0)))

        # --- 权限(每拍都刷: 用户正在系统设置里改, 得马上看到进度) ---
        ax_ok = bool(get("ax"))
        in_ok = bool(get("running")) and int(get("rc", 0)) == 0
        # 权限完成态变了 -> 权限区要在「分步引导」和「一行」之间切换, 得整页重排。
        # (_relayout 会重新进来刷一遍, 这里先 return 免得碰已经不在的控件。)
        if all(s["ok"] for s in perm_steps(ax_ok, in_ok)) != self._laid_perm_done:
            self._relayout()
            return
        steps = perm_steps(ax_ok, in_ok)
        txt, done, total = perm_summary(ax_ok, in_ok)
        if "perm_sum" in self.c:            # 概览页才有这一行
            self.c["perm_sum"].setStringValue_(txt)
        need_re = needs_relaunch(bool(getattr(a, "_boot_ax", False)),
                                bool(getattr(a, "_boot_input", False)), ax_ok, in_ok)
        if done != total and "p_ax_dot" in self.c:
            # 未完成: 分步引导那套控件在 (只有概览页、且未完成时才画)。
            for s in steps:
                k = s["key"]
                self.c["p_%s_dot" % k].setStringValue_(perm_glyph(s["state"]))
                self.c["p_%s_dot" % k].setTextColor_(
                    _color("systemGreenColor") if s["state"] == "done"
                    else (_color("labelColor") if s["state"] == "next"
                          else _color("tertiaryLabelColor")))
                title = "第 %d 步 · %s" % (s["n"], s["title"])
                self.c["p_%s_title" % k].setStringValue_(
                    title if s["state"] != "done" else s["title"])
                self.c["p_%s_title" % k].setTextColor_(
                    _color("secondaryLabelColor") if s["state"] in ("done", "wait")
                    else _color("labelColor"))
                why = s["why"]
                if s["state"] == "done":
                    why = "已授权"
                elif s["state"] == "wait":
                    why = "上一步完成后才能进行"
                self.c["p_%s_why" % k].setStringValue_(why)
                btn = self.c["p_%s_btn" % k]
                btn.setTitle_("已完成" if s["state"] == "done" else "打开设置")
                btn.setEnabled_(s["state"] == "next")
            if "hint" in self.c:
                if need_re:
                    self.c["hint"].setStringValue_("改动需要重启本程序才生效")
                    self.c["hint"].setTextColor_(_color("systemOrangeColor"))
                else:
                    self.c["hint"].setStringValue_("")
        if "relaunch" in self.c:            # 只有「权限已完成」的版式里才有这颗按钮
            self.c["relaunch"].setHidden_(not need_re)

        # --- 开关与下拉(借用挡板, 免得设置动作反过来写一遍) ---
        binds = HB.binds_from_cfg(a.cfg)
        # ★每一页只画自己的控件 —— 这里全部按"存在才写"来 (切页 = 整窗重建)
        _on = str(a.cfg.get("input_mode", "auto")) in ("auto", "touch")
        self._syncing = True
        try:
            for _k, _v in (("enable", 1 if a.enabled else 0),
                           ("debug", 1 if a.cfg["debug_log"] else 0),
                           ("enhanced", 1 if _on else 0)):
                if _k in self.c:
                    self.c[_k].setState_(_v)
            if "enhanced_hint" in self.c:       # 概览页那格状态字
                self.c["enhanced_hint"].setStringValue_(self._enhanced_hint(a))
            for g, _t, opts, _d in BIND_ROWS:
                if ("bind_%s" % g) in self.c:
                    self._select(self.c["bind_%s" % g], list(opts), binds.get(g))
            if "nat" in self.c:
                self._select(self.c["nat"], [k for k, _ in NAT_LABELS], a.cfg["natural"])
                from dptouch_engine import GAINS
                self._select(self.c["gain"], [g for g, _ in GAINS], a.cfg["gain"])
        finally:
            self._syncing = False
        if "nat" in self.c:
            # 滚动方向 / 速度: 只有「没有手势绑滚动」时才没意义 (用悬停提示说, 不占一行)
            usable = "scroll" in binds.values()
            for _k in ("nat", "gain"):
                self.c[_k].setEnabled_(usable)
                self.c[_k].setToolTip_(None if usable else "没有手势绑「滚动」")

        # 窗口起来了却还没成为 key: 再抢几次(菜单收起那一刻的激活请求系统会忽略)。
        # 抢到就停, 免得一直跟用户抢焦点。
        if self._focus_left > 0:
            if self.win.isKeyWindow():
                self._focus_left = 0
            else:
                self._focus_left -= 1
                try:
                    self._activate()
                except Exception:
                    self._focus_left = 0

        # --- 自启 / 显示器: 都是系统查询, 别每拍都问 (每 10 拍或 force 一次) ---
        if force or self._n % 10 == 1:
            try:
                s = a.autostart_status()
            except Exception as e:
                s = {"enabled": False, "detail": "状态读不出来: %r" % (e,)}
            if "autostart" in self.c:
                self.c["autostart"].setState_(1 if s["enabled"] else 0)
            self._sync_display()
        self.c["ver"].setStringValue_("v%s · MIT" % self.version)
        # --- 侧栏设备区 / 底条 / 概览页的实时字段 ---
        _d = devs[0] if devs else None
        if "dev_name" in self.c:
            self.c["dev_name"].setStringValue_(_d[0] if _d else "未连接平板")
        if "dev_art" in self.c:
            self.c["dev_art"].on = bool(_d)            # 没连上就压暗
            self.c["dev_art"].setNeedsDisplay_(True)
        _tgt_wh = getattr(self, "_disp_wh", None) or (None, None)
        if "dev_meta" in self.c:
            self.c["dev_meta"].setStringValue_(
                ("%d × %d · 触控就绪" % _tgt_wh) if all(_tgt_wh) else "插上 USB-C 开始")
        if "live_tag" in self.c:
            self.c["live_tag"].setStringValue_("● 已连接" if _d else "○ 未连接")
            self.c["live_tag"].setTextColor_(_color("systemGreenColor") if _d
                                             else _color("tertiaryLabelColor"))
        if "mini_disp" in self.c:
            self.c["mini_disp"].setStringValue_("正常" if _d else "—")
            self.c["mini_touch"].setStringValue_("可用" if get("touch_iface") else "未检测到")
            self.c["mini_events"].setStringValue_("%d" % get("events", 0))
        if "strip" in self.c:
            self.c["strip"].setStringValue_(
                ("设备在线 · %s" % _d[0]) if _d else "等待平板接入")
            self.c["strip_dot"].setTextColor_(_color("systemGreenColor") if _d
                                              else _color("tertiaryLabelColor"))
        if "ov_disp" in self.c:
            self.c["ov_disp"].setStringValue_(("%d × %d" % _tgt_wh) if all(_tgt_wh)
                                              else "未识别到平板屏")
            self.c["ov_disp_sub"].setStringValue_(
                ("像素 1:1 · 最锐模式" if all(_tgt_wh)
                 and getattr(self, "_disp_name", "") else "") if all(_tgt_wh) else "")
        if "diag_touch" in self.c:
            self.c["diag_touch"].setStringValue_(
                "已识别触摸屏接口" if get("touch_iface") else "未检测到触摸屏接口 · 平板侧需 root")
        for _g, _t, _o, _dd in BIND_ROWS:
            if ("sum_%s" % _g) in self.c:
                self.c["sum_%s" % _g].setStringValue_(HB.action_title(_g, binds.get(_g)))
        # (旧的「详情」按钮已被右上角图标按钮取代, 见 onDetails_)

    @objc.python_method
    def _sys_natural(self):
        try:
            from dptouch_engine import system_natural_scrolling
            return bool(system_natural_scrolling())
        except Exception:
            return False

    @objc.python_method
    def _sync_display(self):
        """把「缩放档位」下拉与显示器现状对齐。

        档位列表来自 app._disp_opts(菜单那份, 唯一出处), 只在档位集合真的变了时重建
        —— 反复 removeAllItems+addItems 会让正在拉开的菜单失焦。
        """
        import dptouch_display as DP
        # ★概览/运行页没有档位下拉, 但侧栏和概览要读"当前档位" —— 缓存必须**在早退之前**
        #   更新, 否则那两页永远显示"未识别到平板屏"(实测踩过)。
        has_popup = "disp" in self.c
        opts = list(getattr(self.app, "_disp_opts", []) or [])
        tgt = getattr(self.app, "_disp_tgt", None)
        sig = tuple((o["w"], o["h"]) for o in opts)
        if tgt is None:
            self._disp_wh = None
            self._disp_name = ""
            if has_popup:
                self.c["disp_hint"].setStringValue_("未识别到平板屏，这里不动任何显示设置")
                if self._disp_sig is not None:
                    self.c["disp"].removeAllItems()
                    self.c["disp"].addItemsWithTitles_(["未识别到平板屏"])
                    self._disp_sig = None
            return
        panel = DP.panel_native(tgt)
        cur = DP.current(tgt["id"])
        self._disp_wh = (cur.get("w"), cur.get("h"))
        self._disp_name = tgt.get("name") or ""
        if not has_popup:
            return
        if sig != self._disp_sig:
            self._disp_sig = sig
            self.c["disp"].removeAllItems()
            self.c["disp"].addItemsWithTitles_([DP.label_short(o, panel) for o in opts] or ["没有可选档位"])
        # (cur 与缓存已在上面算过, 这里直接用)
        # 正常时不占视线 (「当前 UI / 帧缓冲…」那串挪到高级选项了); 只有出问题才在这说一句
        self.c["disp_hint"].setStringValue_("")
        self._syncing = True
        try:
            for i, o in enumerate(opts):
                if (o["w"], o["h"]) == (cur["w"], cur["h"]):
                    self.c["disp"].selectItemAtIndex_(i)
                    break
        finally:
            self._syncing = False

    # ---------------- 交互(一律转交 app, 不在窗口里另留一份状态) ----------------
    def onPerm_(self, sender):
        self.app.perm_step(int(sender.tag()))
        self.refresh(force=True)

    def onRecheck_(self, sender):
        self.app.actRecheck_(None)
        self.refresh(force=True)

    def onRelaunch_(self, sender):
        self.app.actRelaunch_(None)

    def onEnable_(self, sender):
        self.app.actToggleEnable_(None)
        self.refresh(force=True)

    @objc.python_method
    def _enhanced_hint(self, a):
        """增强模式右边那行小字: 开着报实测状态, 关着说明它是"只认笔"。"""
        if str(a.cfg.get("input_mode", "auto")) not in ("auto", "touch"):
            return "已关 · 只认笔接口（与 1.1 相同）"
        try:
            if a.engine.touch_iface:
                return "已检测到触摸屏接口 · 手指可直接点击 / 拖拽"
            return "未检测到触摸屏接口 · 平板侧需 root 解锁"
        except Exception:
            return "已开启"

    def onDetails_(self, sender):
        """右上「详情」: 展开/收起计数行。会改卡片高度, 所以整页重排。"""
        self._details = not self._details
        self._relayout()

    def onEnhanced_(self, sender):
        """「增强模式」开关: 开 = 连触摸屏接口一起认（手指可用）, 关 = 只认笔（1.1 行为）。"""
        self.app.actToggleEnhanced_(None)
        self.refresh(force=True)

    def onTarget_(self, sender):
        """高级选项里的「光标基准屏」下拉 —— 值按下标回到 app._tgt_opts 里取。"""
        if self._syncing:
            return
        opts = list(getattr(self.app, "_tgt_opts", []) or [])
        i = max(0, int(sender.indexOfSelectedItem()))
        if i < len(opts):
            self.app.set_target(opts[i][0])
        self.refresh_all()

    def onDragPen_(self, sender):
        self.app.actDragPen_(None)
        self.refresh_all()

    def onUnknown_(self, sender):
        self.app.actUnknown_(None)
        self.refresh_all()

    def onBind_(self, sender):
        """手势下拉: 第 tag 行 (BIND_ROWS 的顺序) 选了第 index 个动作。"""
        if self._syncing:
            return
        i = max(0, int(sender.tag()))
        if i >= len(BIND_ROWS):
            return
        g, _t, opts, _d = BIND_ROWS[i]
        self.app.set_bind(g, opts[max(0, int(sender.indexOfSelectedItem()))])
        self.refresh_all()

    def onResetBinds_(self, sender):
        self.app.reset_binds()
        self.refresh_all()

    def onAdvanced_(self, sender):
        self.adv.show()
        self.refresh_all()

    def onHoldMs_(self, sender):
        if self._syncing:
            return
        self.app.set_hold_ms(_HOLD_MS[max(0, sender.indexOfSelectedItem())])
        self.refresh_all()

    def onRcHold_(self, sender):
        if self._syncing:
            return
        self.app.set_rc_hold(_RC_HOLD[max(0, sender.indexOfSelectedItem())])
        self.refresh_all()

    def onDispAllow_(self, sender):
        self.app.actDispAllow_(None)
        self.refresh_all()

    def onUpdateAuto_(self, sender):
        self.app.actUpdateAuto_(None)
        self.refresh_all()

    def onCheckUpdate_(self, sender):
        """手动查一次新版本。查到/查不到/查不动都由 app 那边弹窗说明。"""
        self.app.actCheckUpdate_(None)

    def onDebug_(self, sender):
        self.app.actDebugLog_(None)
        self.refresh(force=True)

    def onAutostart_(self, sender):
        self.app.actAutostart_(None)
        self.refresh(force=True)

    def onNat_(self, sender):
        if self._syncing:
            return
        self.app.set_natural(NAT_LABELS[max(0, sender.indexOfSelectedItem())][0])
        self.refresh(force=True)

    def onGain_(self, sender):
        if self._syncing:
            return
        from dptouch_engine import GAINS
        i = max(0, sender.indexOfSelectedItem())
        self.app.set_gain(GAINS[min(i, len(GAINS) - 1)][0])
        self.refresh(force=True)

    def onDisp_(self, sender):
        if self._syncing:
            return
        self.app.apply_display_index(max(0, sender.indexOfSelectedItem()))
        self.refresh(force=True)

    def onDispRescan_(self, sender):
        self.app.actDisplayRescan_(None)
        self.refresh(force=True)

    def onLog_(self, sender):
        self.app.actOpenLog_(None)

    def onDiag_(self, sender):
        self.app.actDiagnostics_(None)


# ---------------------------------------------------------------- 高级选项

class AdvancedWindow(_Pane):
    """高级选项 —— 不常用的那几项都搬这儿, 主窗口只留常用开关。

    为什么是独立小窗而不是折叠块: 主窗口的 frame 是手算的 (见模块头), 折叠会让整页
    重排; 而且「高级」本来就该离常用开关远一点, 免得两边互相干扰。

    状态不在这里另存一份: 控件全指向 SettingsWindow 的那几个动作, 读一律读 app.cfg。
    """

    def initWithOwner_(self, owner):
        self = objc.super(AdvancedWindow, self).init()
        if self is None:
            return None
        self.owner = owner
        self.app = owner.app
        self._W, self._H = AW, AH
        self.c = {}
        self._syncing = False
        self._build()
        return self

    @objc.python_method
    def _build(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, AW, AH), style, NSBackingStoreBuffered, False)
        self.win.setTitle_("高级选项")
        self.win.setReleasedWhenClosed_(False)
        self.win.center()
        v = self.win.contentView()
        cx = M + PAD
        y = 12.0
        cw = AW - M - PAD - cx                 # 控件可用宽度
        # 末尾 52pt = 「立即检查更新 / 诊断…」那一行 + 平板屏参数那行
        h = len(ADV_SWITCHES) * CB_H + len(ADV_POPUPS) * ROW_H + 52 + 2 * PAD
        self._card(v, y, h)
        for i, (key, _cfg, title, act) in enumerate(ADV_SWITCHES):
            self.c[key] = _button(title, self._r(cx, y + PAD + i * CB_H + 3, cw, 22),
                                  self.owner, act, switch=True)
            v.addSubview_(self.c[key])
        ry = y + PAD + len(ADV_SWITCHES) * CB_H
        for key, _cfg, title, act in ADV_POPUPS:
            v.addSubview_(_label(title, self._r(cx, ry + 5, 100, 18)))
            if key == "target":
                titles = [t for _v, t in self.app.target_choices()]
                wide = 348            # 屏名 + 分辨率比时长档长得多
            elif key == "hold_ms":
                titles, wide = list(HOLD_TITLES), 130
            else:
                titles, wide = list(RC_TITLES), 130
            pop = _popup(self._r(cx + 104, ry + 1, wide, 26), titles)
            pop.setTarget_(self.owner)
            pop.setAction_(act)
            v.addSubview_(pop)
            self.c[key] = pop
            if key == "target":
                self._tgt_titles = list(titles)
            ry += ROW_H
        by = ry + 6            # ry 已经在 popups 循环里走到它们下面了, 别再乘一遍
        self.c["check_upd"] = _button("立即检查更新", self._r(cx, by, 128, 24),
                                      self.owner, "onCheckUpdate:")
        v.addSubview_(self.c["check_upd"])
        # 「诊断…」从主窗口搬过来: 排查用的东西, 平时用不上
        self.c["diag_btn"] = _button("诊断…", self._r(cx + 136, by, 88, 24),
                                     self.owner, "onDiag:", small=True)
        v.addSubview_(self.c["diag_btn"])
        self.c["hint"] = _label("改完立即生效", self._r(cx + 232, by + 5, cw - 232, 16),
                                size=11, dim=True)
        v.addSubview_(self.c["hint"])
        # 平板屏当前参数 (原来挂在主窗口「显示缩放」下面, 是排查信息, 搬这儿)
        self.c["disp_info"] = _label("", self._r(cx, by + 30, cw, 16), size=11, dim=True)
        v.addSubview_(self.c["disp_info"])
        return True

    @objc.python_method
    def show(self):
        if self.win is None:
            return False
        self.refresh(force=True)
        try:
            self.owner._activate()          # 菜单栏 App 的窗口要自己抢一次焦点, 否则下拉是灰的
        except Exception:
            pass
        self.win.makeKeyAndOrderFront_(None)
        self.win.orderFrontRegardless()
        try:                                # 贴在主窗口右边; 主窗口不在屏幕上就居中
            mf = self.owner.win.frame()
            if mf.size.width > 1 and mf.size.height > 1:
                self.win.setFrameOrigin_((mf.origin.x + mf.size.width + 12,
                                          mf.origin.y + mf.size.height - AH))
            else:
                self.win.center()
        except Exception:
            self.win.center()
        self.app.log("高级选项: 已打开")
        return True

    @objc.python_method
    def hide(self):
        if self.win is not None and self.win.isVisible():
            self.win.orderOut_(None)
            return True
        return False

    @objc.python_method
    def is_visible(self):
        return bool(self.win is not None and self.win.isVisible())

    @objc.python_method
    def refresh(self, force=False):
        """控件拉回 app.cfg 的真值 (借用挡板, 免得设置动作反过来把状态写一遍)。"""
        if self.win is None:
            return
        if not force and not self.win.isVisible():
            return
        a = self.app
        self._syncing = True
        try:
            for key, cfg, _t, _act in ADV_SWITCHES:
                self.c[key].setState_(1 if a.cfg.get(cfg) else 0)
            self._sync_target_popup(a)
            self._select(self.c["hold_ms"], list(_HOLD_MS), int(a.cfg.get("hold_ms") or 250))
            self._select(self.c["rc_hold"], list(_RC_HOLD), int(a.cfg.get("rc_hold_ms") or 0))
            # 平板屏当前参数 (只读; 原来在主窗口显示缩放下面)
            try:
                import dptouch_display as DP
                tgt = getattr(a, "_disp_tgt", None)
                if tgt:
                    cur = DP.current(tgt["id"])
                    self.c["disp_info"].setStringValue_(
                        "平板屏 %d×%d UI · %d×%d 帧缓冲 · %gHz · %s"
                        % (cur["w"], cur["h"], cur["pw"], cur["ph"], cur["hz"],
                           "HiDPI" if cur["hidpi"] else "1x"))
                else:
                    self.c["disp_info"].setStringValue_("未识别到平板屏")
            except Exception:
                self.c["disp_info"].setStringValue_("")
        finally:
            self._syncing = False

    @objc.python_method
    def _sync_target_popup(self, a):
        """光标基准屏: 屏插拔过就重建条目, 再按配置值选中 (值对不上退到第一项)。"""
        pop, opts = self.c.get("target"), list(a.target_choices())
        if pop is None:
            return
        titles = [t for _v, t in opts]
        if titles != getattr(self, "_tgt_titles", None):
            pop.removeAllItems()
            pop.addItemsWithTitles_(titles)
            self._tgt_titles = titles
        cur = str(a.cfg.get("target_display") or "auto")
        pop.selectItemAtIndex_(next((i for i, (v, _t) in enumerate(opts) if v == cur), 0))
