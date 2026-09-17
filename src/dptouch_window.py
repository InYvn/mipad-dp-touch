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
    NSAttributedString, NSBackingStoreBuffered, NSBundle, NSButton, NSColor, NSFont,
    NSFontAttributeName, NSImage, NSImageView, NSMakeRect, NSObject, NSPopUpButton,
    NSTextField, NSWindow,
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
    """一块圆角分组底(纯装饰, 不接收点击)。"""

    def drawRect_(self, rect):
        b = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), CARD_R, CARD_R)
        _color("controlBackgroundColor").set()
        b.fill()


from hid_bridge import HOLD_MS_OPTS as _HOLD_MS   # 长按判定档位(ms), 与 CLI 同源
from hid_bridge import RC_HOLD_MS_OPTS as _RC_HOLD   # 长按不动=右键 的档位(ms), 与 CLI 同源

W, H = 620.0, 918.0                         # 窗口内容尺寸(固定; 不设 Resizable)
M = 22.0                                    # 页面左右留白
PAD = 12.0                                  # 卡片内边距
LBL_W = 94.0                                # 标签列宽
CTRL_X = M + PAD + LBL_W                    # 下拉框那一列的左边缘 (128)
CTRL_W = 250.0                              # 下拉框标准宽
HINT_X = CTRL_X + CTRL_W + 8                # 行尾说明那列 (386)
CR = W - M - PAD                            # 卡片内容右边线 (586)
ROW_H = 28.0                                # 「标签 + 控件」一行的高度
CB_H = 26.0                                 # 复选框一行的高度
SEC_GAP = 16.0                              # 卡片之间的间距

# UI 文案 —— 短句, 不用括号。菜单里不再出现这些选项, 这里是唯一出处。
MODE_LABELS = [("scroll", "滑动翻页 · 不选中文字"),
               ("select", "滑动选择 · 拖拽与划选")]
NAT_LABELS = [("system", "跟随系统"), ("on", "始终自然"), ("off", "始终传统")]
HOLD_TITLES = ["%.2gs %s" % (ms / 1000.0, lab)
               for ms, lab in zip(_HOLD_MS, ("极快", "快", "标准", "慢"))]
RC_TITLES = ["关" if not ms else "%.1f 秒" % (ms / 1000.0) for ms in _RC_HOLD]

# 分步授权的两条 (顺序就是展示顺序)
PERM_STEPS = (
    ("ax", "辅助功能", "允许合成点击与拖拽，否则笔点了没反应"),
    ("input", "输入监控", "读取平板笔的原始数据，否则收不到任何笔事件"),
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


def _hit(a, b, slack=0.5):
    """两个矩形是否真重叠 —— 只挨着不算(布局里行与行本来就是贴着的)。"""
    return (a[0] + a[2] - slack > b[0] and b[0] + b[2] - slack > a[0]
            and a[1] + a[3] - slack > b[1] and b[1] + b[3] - slack > a[1])


def check_layout(view, w=W, h=H):
    """离线 GUI 质检: 控件越界 / 互相重叠 / 文字被切。

    我看不到屏幕, 所以"排得对不对"只能这样验: 每个控件的 frame 与它自己那句
    文字用 AppKit 量出来的宽度比。返回问题列表(空 = 干净)。
    """
    items = []
    for v in view.subviews():
        try:
            if v.identifier() in ("bg", "line"):
                continue        # 分组底本来就垫在控件底下, 不算"重叠"
        except Exception:
            pass
        txt = ""
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
                bad.append("重叠: %r <-> %r" % (items[i][2][:16], items[j][2][:16]))
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
        if v.__class__.__name__ == "NSButton":
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
        try:
            if v.isHidden():
                txt = txt + "   [隐藏]"
        except Exception:
            pass
        r = _rect(v)
        print("  %-7.0f,%-6.0f %-5.0fx%-5.0f %-26s %s"
              % (r[0], r[1], r[2], r[3], v.__class__.__name__, txt[:46]), flush=True)


# ---------------------------------------------------------------- 窗口

class SettingsWindow(NSObject):
    """设置窗口。常驻一个实例, 反复开关只做 orderFront_ / orderOut_。"""

    def initWithApp_appName_version_(self, app, name, version):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self.app = app
        self.name = name or ""
        self.version = version or ""
        self.c = {}                          # 控件登记表
        self._syncing = False
        self._n = 0
        self._focus_left = 0            # 窗口刚起来时还欠几次"抢焦点"
        self._disp_sig = None
        self._build()
        return self

    # ---------------- 坐标: 按"从上往下"写, 这里翻成 AppKit 的从左下角 ----------------
    @objc.python_method
    def _r(self, x, y_top, w, h):
        return NSMakeRect(x, H - y_top - h, w, h)

    @objc.python_method
    def _sec(self, v, text, y_top, note=None):
        """一行分组标题, 返回它下面那块卡片的起始 y。"""
        v.addSubview_(_label(text, self._r(M, y_top, 240, 17), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        if note:
            v.addSubview_(_label(note, self._r(CR - 300, y_top, 300, 16),
                                 size=11, dim=True, right=True))
        return y_top + 22

    @objc.python_method
    def _card(self, v, y_top, h):
        """垫在控件底下的一块圆角分组底。要先加, 才在控件下面。"""
        b = _CardView.alloc().initWithFrame_(self._r(M, y_top, W - 2 * M, h))
        b.setIdentifier_("bg")
        v.addSubview_(b)
        return b

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
        v = self.win.contentView()
        cx = M + PAD                             # 卡片内容的左边线
        cbh = 26.0                               # 复选框一行的高度
        gap = 16.0                               # 两个分组之间

        # ---- 头部: 左边 logo 和名字, 右边一眼看得到的状态 ----
        y = 12.0
        self.c["logo"] = logo_view(logo_path())
        self.c["logo"].setFrame_(self._r(M, y, LOGO_PX, LOGO_PX))
        v.addSubview_(self.c["logo"])
        v.addSubview_(_label(self.name, self._r(M + 68, y + 6, 240, 20), size=15, bold=True))
        v.addSubview_(_label("小米焦点触控笔 Pro · 当 Mac 外接屏时的输入桥接",
                             self._r(M + 68, y + 28, 250, 15), size=11, dim=True))
        self.c["conn_dot"] = _label("●", self._r(CR - 244, y + 7, 14, 17), size=13,
                                    color=_color("tertiaryLabelColor"))
        v.addSubview_(self.c["conn_dot"])
        self.c["conn"] = _label("正在检测…", self._r(CR - 226, y + 7, 226, 17))
        v.addSubview_(self.c["conn"])
        self.c["counters"] = _label("", self._r(CR - 230, y + 27, 230, 15), size=11, dim=True)
        v.addSubview_(self.c["counters"])
        y += LOGO_PX + 8

        # ---- 权限: 分步引导 (按钮搬到标题行右侧, 卡片里只留两步) ----
        v.addSubview_(_label("权限", self._r(M, y + 4, 50, 18), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        self.c["perm_sum"] = _label("", self._r(M + 52, y + 5, 200, 16),
                                    size=11, dim=True)
        v.addSubview_(self.c["perm_sum"])
        self.c["relaunch"] = _button("立即重启", self._r(CR - 104, y, 104, 26),
                                     self, "onRelaunch:")
        v.addSubview_(self.c["relaunch"])
        self.c["recheck"] = _button("重新检测", self._r(CR - 218, y, 104, 26),
                                    self, "onRecheck:")
        v.addSubview_(self.c["recheck"])
        self._card(v, y + 32, 132)
        cyy = y + 32
        for i, key in ((0, "ax"), (1, "input")):
            ry = cyy + PAD + i * 44
            self.c["p_%s_dot" % key] = _label("1", self._r(cx, ry + 12, 20, 16), size=12, bold=True)
            v.addSubview_(self.c["p_%s_dot" % key])
            self.c["p_%s_title" % key] = _label("", self._r(cx + 22, ry + 6, 250, 16),
                                                size=13, bold=True)
            v.addSubview_(self.c["p_%s_title" % key])
            self.c["p_%s_why" % key] = _label("", self._r(cx + 22, ry + 26, 420, 15),
                                              size=11, dim=True)
            v.addSubview_(self.c["p_%s_why" % key])
            self.c["p_%s_btn" % key] = _button("打开设置", self._r(CR - 104, ry + 12, 104, 26),
                                               self, "onPerm:")
            self.c["p_%s_btn" % key].setTag_(i)
            v.addSubview_(self.c["p_%s_btn" % key])
        self.c["hint"] = _label("", self._r(cx, cyy + PAD + 92, CR - cx, 16), size=11, dim=True)
        v.addSubview_(self.c["hint"])
        cy = cyy + 132

        # ---- 笔的输入 ----
        cy = self._sec(v, "笔的输入", cy + gap)
        self._card(v, cy, 5 * ROW_H + 2 * PAD)
        ry = cy + PAD
        v.addSubview_(_label("滑动方式", self._r(cx, ry + 5, LBL_W, 18)))
        self.c["mode"] = _popup(self._r(CTRL_X, ry + 1, CTRL_W, 26), [t for _, t in MODE_LABELS])
        self.c["mode"].setTarget_(self)
        self.c["mode"].setAction_("onMode:")
        v.addSubview_(self.c["mode"])

        # 触屏模式下的第二个手势: 快速划=滚动, 停住再划=拖拽
        ry = cy + PAD + ROW_H
        self.c["hold"] = _button("停住再划 = 拖拽", self._r(cx, ry + 4, 176, 20),
                                 self, "onHold:", switch=True)
        v.addSubview_(self.c["hold"])
        self.c["hold_ms"] = _popup(self._r(CTRL_X + CTRL_W - 110, ry + 1, 110, 26), list(HOLD_TITLES))
        self.c["hold_ms"].setTarget_(self)
        self.c["hold_ms"].setAction_("onHoldMs:")
        v.addSubview_(self.c["hold_ms"])
        self.c["hold_hint"] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                     size=11, dim=True, right=True)
        v.addSubview_(self.c["hold_hint"])

        # 触屏模式下的第三个手势: 停在原地不动 -> 抬手弹右键菜单。
        # 与上面那条不冲突: 「停住再划」= 拖拽, 「停住不划」= 右键。
        ry = cy + PAD + 2 * ROW_H
        v.addSubview_(_label("长按不动", self._r(cx, ry + 5, LBL_W, 18)))
        self.c["rc_hold"] = _popup(self._r(CTRL_X, ry + 1, 130, 26), list(RC_TITLES))
        self.c["rc_hold"].setTarget_(self)
        self.c["rc_hold"].setAction_("onRcHold:")
        v.addSubview_(self.c["rc_hold"])
        self.c["rc_hint"] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                   size=11, dim=True, right=True)
        v.addSubview_(self.c["rc_hint"])

        ry = cy + PAD + 3 * ROW_H
        v.addSubview_(_label("滚动方向", self._r(cx, ry + 5, LBL_W, 18)))
        self.c["nat"] = _popup(self._r(CTRL_X, ry + 1, CTRL_W, 26), [t for _, t in NAT_LABELS])
        self.c["nat"].setTarget_(self)
        self.c["nat"].setAction_("onNat:")
        v.addSubview_(self.c["nat"])
        self.c["nat_hint"] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                    size=11, dim=True, right=True)
        v.addSubview_(self.c["nat_hint"])

        ry = cy + PAD + 4 * ROW_H
        v.addSubview_(_label("滚动速度", self._r(cx, ry + 5, LBL_W, 18)))
        self.c["gain"] = _popup(self._r(CTRL_X, ry + 1, CTRL_W, 26), self._gain_titles())
        self.c["gain"].setTarget_(self)
        self.c["gain"].setAction_("onGain:")
        v.addSubview_(self.c["gain"])
        self.c["gain_hint"] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                     size=11, dim=True, right=True)
        v.addSubview_(self.c["gain_hint"])
        cy += 5 * ROW_H + 2 * PAD


        # ---- 桥接 ----
        cy = self._sec(v, "桥接", cy + gap)
        self._card(v, cy, 3 * cbh + 2 * PAD)
        for i, (key, title, act) in enumerate((("enable", "启用笔桥接", "onEnable:"),
                                               ("takeover", "用笔的绝对坐标驱动光标（试验）",
                                                "onTakeover:"),
                                               ("unknown", "允许未验证的小米设备", "onUnknown:"))):
            self.c[key] = _button(title, self._r(cx, cy + PAD + i * cbh + 3, 360, 22),
                                  self, act, switch=True)
            v.addSubview_(self.c[key])
        cy += 3 * cbh + 2 * PAD

        # ---- 显示缩放 ----
        cy = self._sec(v, "显示缩放", cy + gap, note="只改平板那块屏，其他屏不动")
        self._card(v, cy, 100)
        ry = cy + PAD
        v.addSubview_(_label("缩放档位", self._r(cx, ry + 5, LBL_W, 18)))
        self.c["disp"] = _popup(self._r(CTRL_X, ry + 1, 340, 26), ["正在扫描…"])
        self.c["disp"].setTarget_(self)
        self.c["disp"].setAction_("onDisp:")
        v.addSubview_(self.c["disp"])
        self.c["disp_btn"] = _button("重新扫描", self._r(CR - 104, ry + 1, 104, 26),
                                     self, "onDispRescan:")
        v.addSubview_(self.c["disp_btn"])
        self.c["disp_hint"] = _label("", self._r(cx, ry + 32, CR - cx, 16), size=11, dim=True)
        v.addSubview_(self.c["disp_hint"])
        self.c["disp_allow"] = _button("允许管理未实测的显示器", self._r(cx, ry + 55, 360, 22),
                                       self, "onDispAllow:", switch=True)
        v.addSubview_(self.c["disp_allow"])
        cy += 100

        # ---- 启动与日志 ----
        cy = self._sec(v, "启动与日志", cy + gap)
        self._card(v, cy, 2 * ROW_H + 2 * PAD)
        ry = cy + PAD
        self.c["autostart"] = _button("登录时自动启动", self._r(cx, ry + 2, 170, 22),
                                      self, "onAutostart:", switch=True)
        v.addSubview_(self.c["autostart"])
        self.c["autostart_hint"] = _label("", self._r(cx + 178, ry + 5, CR - cx - 178, 18),
                                          size=11, dim=True, right=True)
        v.addSubview_(self.c["autostart_hint"])
        ry = cy + PAD + ROW_H
        self.c["debug"] = _button("详细日志", self._r(cx, ry + 2, 130, 22),
                                  self, "onDebug:", switch=True)
        v.addSubview_(self.c["debug"])
        self.c["log_btn"] = _button("打开日志", self._r(CR - 212, ry, 104, 26),
                                    self, "onLog:", small=True)
        v.addSubview_(self.c["log_btn"])
        self.c["diag_btn"] = _button("诊断…", self._r(CR - 104, ry, 104, 26),
                                     self, "onDiag:", small=True)
        v.addSubview_(self.c["diag_btn"])
        cy += 2 * ROW_H + 2 * PAD

        # ---- 页脚: 只有版本行 (「关于」「退出」已在菜单栏里) ----
        self.c["ver"] = _label("", self._r(cx, cy + 14, CR - cx, 16), size=11, dim=True)
        v.addSubview_(self.c["ver"])
        used = cy + 14 + 16 + 12
        if abs(used - H) > 6:
            try:
                self.app.log("窗口高度对不上: 布局用到 %g, H=%g" % (used, H))
            except Exception:
                pass
        return True

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
        return True

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
        if not force and not self.win.isVisible():
            return
        try:
            self._refresh(st, force)
        except Exception as e:              # 定时器回调里出错不能静默
            self.app.log("设置窗口刷新失败: %r" % (e,))

    @objc.python_method
    def _refresh(self, st, force):
        self._n += 1
        a = self.app
        st = st if st is not None else a.engine.status()
        get = (st or {}).get

        # --- 状态 ---
        devs = get("devices") or []
        names = "、".join(d[0] for d in devs)
        if names:
            self.c["conn"].setStringValue_("已连接 · %s" % names)
            self.c["conn_dot"].setTextColor_(_color("systemGreenColor"))
        elif get("running"):
            self.c["conn"].setStringValue_("已开启 · 等平板上线")
            self.c["conn_dot"].setTextColor_(_color("systemOrangeColor"))
        else:
            self.c["conn"].setStringValue_("未运行")
            self.c["conn_dot"].setTextColor_(_color("systemRedColor"))
        self.c["counters"].setStringValue_(
            "笔事件 %d · 点击 %d · 拖拽 %d · 滚动 %d"
            % (get("events", 0), get("click", 0), get("drag", 0), get("scroll", 0)))

        # --- 权限(每拍都刷: 用户正在系统设置里改, 得马上看到进度) ---
        ax_ok = bool(get("ax"))
        in_ok = bool(get("running")) and int(get("rc", 0)) == 0
        steps = perm_steps(ax_ok, in_ok)
        txt, done, total = perm_summary(ax_ok, in_ok)
        self.c["perm_sum"].setStringValue_(txt)
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
        need_re = needs_relaunch(bool(getattr(a, "_boot_ax", False)),
                                bool(getattr(a, "_boot_input", False)), ax_ok, in_ok)
        if need_re:
            self.c["hint"].setStringValue_("改动需要重启本程序才生效")
            self.c["hint"].setTextColor_(_color("systemOrangeColor"))
        elif done == total:
            self.c["hint"].setStringValue_("权限已就绪")
            self.c["hint"].setTextColor_(_color("systemGreenColor"))
        else:
            self.c["hint"].setStringValue_("")
        self.c["relaunch"].setHidden_(not need_re)

        # --- 开关与下拉(借用挡板, 免得设置动作反过来写一遍) ---
        self._syncing = True
        try:
            self.c["enable"].setState_(1 if a.enabled else 0)
            self.c["takeover"].setState_(1 if a.cfg["takeover"] else 0)
            self.c["unknown"].setState_(1 if a.cfg["allow_unknown"] else 0)
            self.c["disp_allow"].setState_(1 if a.cfg["display_allow_unknown"] else 0)
            self.c["debug"].setState_(1 if a.cfg["debug_log"] else 0)
            self.c["hold"].setState_(1 if a.cfg.get("hold_drag") else 0)
            self._select(self.c["hold_ms"], list(_HOLD_MS), a.cfg.get("hold_ms", 250))
            self._select(self.c["rc_hold"], list(_RC_HOLD), int(a.cfg.get("rc_hold_ms") or 0))
            self._select(self.c["mode"], [k for k, _ in MODE_LABELS], a.cfg["mode"])
            self._select(self.c["nat"], [k for k, _ in NAT_LABELS], a.cfg["natural"])
            from dptouch_engine import GAINS
            self._select(self.c["gain"], [g for g, _ in GAINS], a.cfg["gain"])
        finally:
            self._syncing = False
        # 滚动方向/速度在"滑动选择"或"绝对坐标接管"下不生效
        usable = a.cfg["mode"] == "scroll" and not a.cfg["takeover"]
        self.c["nat"].setEnabled_(usable)
        self.c["gain"].setEnabled_(usable)
        self.c["hold"].setEnabled_(usable)
        self.c["hold_ms"].setEnabled_(usable and bool(a.cfg.get("hold_drag")))
        self.c["rc_hold"].setEnabled_(usable and bool(a.cfg.get("hold_drag")))
        self.c["rc_hint"].setStringValue_("弹出右键菜单" if usable else "仅滑动翻页模式生效")
        self.c["hold_hint"].setStringValue_(self._hold_hint(usable))
        self.c["nat_hint"].setStringValue_(
            "系统当前：%s" % ("自然" if self._sys_natural() else "传统")
            if a.cfg["natural"] == "system" else "")
        self.c["gain_hint"].setStringValue_("" if usable else "仅滑动翻页模式生效")

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
            self.c["autostart"].setState_(1 if s["enabled"] else 0)
            self.c["autostart_hint"].setStringValue_(s.get("detail") or "")
            self._sync_display()
        self.c["ver"].setStringValue_("%s %s · MIT License" % (self.name, self.version))

    @objc.python_method
    def _select(self, popup, keys, value):
        try:
            i = keys.index(value)
        except ValueError:
            try:
                i = min(range(len(keys)), key=lambda k: abs(float(keys[k]) - float(value)))
            except Exception:
                i = 0
        popup.selectItemAtIndex_(i)

    @objc.python_method
    def _hold_hint(self, usable):
        if not usable:
            return "滑动选择模式下每笔都是拖拽"
        return "" if self.app.cfg.get("hold_drag") else "关掉就只有点击和滚动"

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
        opts = list(getattr(self.app, "_disp_opts", []) or [])
        tgt = getattr(self.app, "_disp_tgt", None)
        sig = tuple((o["w"], o["h"]) for o in opts)
        if tgt is None:
            self.c["disp_hint"].setStringValue_("未识别到平板屏，这里不动任何显示设置")
            if self._disp_sig is not None:
                self.c["disp"].removeAllItems()
                self.c["disp"].addItemsWithTitles_(["未识别到平板屏"])
                self._disp_sig = None
            return
        panel = DP.panel_native(tgt)
        if sig != self._disp_sig:
            self._disp_sig = sig
            self.c["disp"].removeAllItems()
            self.c["disp"].addItemsWithTitles_([DP.label_short(o, panel) for o in opts] or ["没有可选档位"])
        cur = DP.current(tgt["id"])
        self.c["disp_hint"].setStringValue_(
            "当前 UI %d×%d · 帧缓冲 %d×%d · %gHz · %s"
            % (cur["w"], cur["h"], cur["pw"], cur["ph"], cur["hz"],
               "HiDPI" if cur["hidpi"] else "1x"))
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

    def onTakeover_(self, sender):
        self.app.actTakeover_(None)
        self.refresh(force=True)

    def onUnknown_(self, sender):
        self.app.actUnknown_(None)
        self.refresh(force=True)

    def onHold_(self, sender):
        self.app.actHold_(None)
        self.refresh(force=True)

    def onHoldMs_(self, sender):
        self.app.set_hold_ms(_HOLD_MS[max(0, sender.indexOfSelectedItem())])
        self.refresh(force=True)

    def onRcHold_(self, sender):
        if self._syncing:
            return
        self.app.set_rc_hold(_RC_HOLD[max(0, sender.indexOfSelectedItem())])
        self.refresh(force=True)

    def onDispAllow_(self, sender):
        self.app.actDispAllow_(None)
        self.refresh(force=True)

    def onDebug_(self, sender):
        self.app.actDebugLog_(None)
        self.refresh(force=True)

    def onAutostart_(self, sender):
        self.app.actAutostart_(None)
        self.refresh(force=True)

    def onMode_(self, sender):
        if self._syncing:
            return
        self.app.set_mode(MODE_LABELS[max(0, sender.indexOfSelectedItem())][0])
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
