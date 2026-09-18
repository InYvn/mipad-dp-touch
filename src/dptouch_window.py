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


import hid_bridge as HB
from hid_bridge import HOLD_MS_OPTS as _HOLD_MS   # 「停住再滑」判定档位(ms), 与 CLI 同源
from hid_bridge import RC_HOLD_MS_OPTS as _RC_HOLD   # 「停住不动」判定档位(ms), 与 CLI 同源

W, H = 620.0, 826.0                         # 主窗口内容尺寸(固定; 不设 Resizable)
AW, AH = 520.0, 262.0                       # 「高级选项」小窗口的内容尺寸
                                            # (多了末尾「立即检查更新」那一行 30pt)
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
NAT_LABELS = [("system", "跟随系统"), ("on", "始终自然"), ("off", "始终传统")]
HOLD_TITLES = ["%.2gs %s" % (ms / 1000.0, lab)
               for ms, lab in zip(_HOLD_MS, ("极快", "快", "标准", "慢"))]
RC_TITLES = ["关" if not ms else "%.1f 秒" % (ms / 1000.0) for ms in _RC_HOLD]

# 主窗口「笔的输入」那张表: 四类手势, 每一类一个下拉 (可绑的动作来自 hid_bridge 那张表,
# 唯一出处 —— 界面里出现的选项一定有实现)。
BIND_ROWS = tuple((g, title, HB.gesture_actions(g), d)
                  for g, title, _o, d in HB.BIND_GESTURES)
SCROLL_ROWS = (("nat", "滚动方向"), ("gain", "滚动速度"))

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
    # 「贴到卡片顶边」也看不出来 —— 用户报过的那次就是: 「恢复默认」按钮底边比卡片
    # 顶边还低 1pt, 不算重叠(重叠那条判据要两边都压过 slack, 1pt 只压了一边, 而且
    # 卡片自己在建 items 时就被跳过了), 但看着就是和下面的框粘在一起。留最小呼吸间距。
    # 注意坐标系: _r() 转出来的 frame 是 AppKit 的 y 向上, 卡片「顶边」= y + h。
    MIN_GAP = 3.5
    _cards = [_rect(_v) for _v in view.subviews() if _ident(_v) == "bg"]
    for v, r, txt in items:
        for _c in _cards:
            _ct = _c[1] + _c[3]                     # 卡片顶边
            _hx = min(r[0] + r[2], _c[0] + _c[2]) - max(r[0], _c[0])
            if _hx <= 0:
                continue                            # 水平上不在卡片上方, 不算
            if r[1] < _ct + MIN_GAP and r[1] + r[3] > _ct:
                bad.append("贴到卡片顶边: %r 底边离卡片顶边只有 %.1fpt (要 >= %gpt)"
                           % (txt[:18], r[1] - _ct, MIN_GAP))
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
    def _card(self, v, y_top, h):
        """垫在控件底下的一块圆角分组底。要先加, 才在控件下面。"""
        b = _CardView.alloc().initWithFrame_(self._r(M, y_top, self._W - 2 * M, h))
        b.setIdentifier_("bg")
        v.addSubview_(b)
        return b

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
        self._build()
        return self

    # ---------------- 坐标: 见 _Pane ----------------
    @objc.python_method
    def _sec(self, v, text, y_top, note=None):
        """一行分组标题, 返回它下面那块卡片的起始 y。"""
        v.addSubview_(_label(text, self._r(M, y_top, 240, 17), size=12, bold=True,
                             color=_color("secondaryLabelColor")))
        if note:
            v.addSubview_(_label(note, self._r(CR - 300, y_top, 300, 16),
                                 size=11, dim=True, right=True))
        return y_top + 22

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

        # ---- 笔的输入: 手势 -> 动作 的绑定表 ----
        cy = self._sec(v, "笔的输入", cy + gap)
        # 「恢复默认」这把小按钮挂在分组标题那一行的右边。它比卡片顶边高一截才算
        # 干净: 原来是 cy-19, 按钮底边 (cy+1) 压在卡片顶边上 1pt, 看着像粘在一起
        # (用户报过)。cy-26 -> 底边 cy-6, 和卡片留 6pt 呼吸, 同时也和标题行对齐。
        self.c["reset_binds"] = _button("恢复默认", self._r(CR - 84, cy - 26, 84, 20),
                                        self, "onResetBinds:", small=True)
        v.addSubview_(self.c["reset_binds"])
        self._card(v, cy, len(BIND_ROWS + SCROLL_ROWS) * ROW_H + 2 * PAD)
        ry = cy + PAD
        for i, (g, title, opts, _d) in enumerate(BIND_ROWS):
            v.addSubview_(_label(title, self._r(cx, ry + 5, LBL_W, 18)))
            pop = _popup(self._r(CTRL_X, ry + 1, CTRL_W, 26),
                         HB.gesture_action_titles(g))
            pop.setTarget_(self)
            pop.setAction_("onBind:")
            pop.setTag_(i)
            v.addSubview_(pop)
            self.c["bind_%s" % g] = pop
            self.c["bind_%s_hint" % g] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                                size=11, dim=True, right=True)
            v.addSubview_(self.c["bind_%s_hint" % g])
            ry += ROW_H
        # 滚动方向 / 速度: 只在有手势绑了「滚动」时才有意义 (所以放同一张卡里, 挨着手势)
        for key, title in SCROLL_ROWS:
            v.addSubview_(_label(title, self._r(cx, ry + 5, LBL_W, 18)))
            items = [t for _v, t in NAT_LABELS] if key == "nat" else self._gain_titles()
            pop = _popup(self._r(CTRL_X, ry + 1, CTRL_W, 26), items)
            pop.setTarget_(self)
            pop.setAction_("onNat:" if key == "nat" else "onGain:")
            v.addSubview_(pop)
            self.c[key] = pop
            self.c["%s_hint" % key] = _label("", self._r(HINT_X, ry + 5, CR - HINT_X, 18),
                                             size=11, dim=True, right=True)
            v.addSubview_(self.c["%s_hint" % key])
            ry += ROW_H
        cy += len(BIND_ROWS + SCROLL_ROWS) * ROW_H + 2 * PAD

        # ---- 显示缩放 ----
        cy = self._sec(v, "显示缩放", cy + gap, note="只改平板那块屏，其他屏不动")
        self._card(v, cy, ROW_H + 16 + 2 * PAD)
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
        cy += ROW_H + 16 + 2 * PAD

        # ---- 运行 (常用开关就这三个, 其余都在「高级选项」里) ----
        cy = self._sec(v, "运行", cy + gap)
        self._card(v, cy, 3 * cbh + 2 * PAD)
        for i, (key, title, act) in enumerate((("enable", "启用笔桥接", "onEnable:"),
                                               ("autostart", "登录时自动启动", "onAutostart:"),
                                               ("debug", "详细日志", "onDebug:"))):
            self.c[key] = _button(title, self._r(cx, cy + PAD + i * cbh + 3, 220, 22),
                                  self, act, switch=True)
            v.addSubview_(self.c[key])
        self.c["autostart_hint"] = _label(
            "", self._r(cx + 228, cy + PAD + cbh + 8, CR - cx - 228, 16), size=11, dim=True,
            right=True)
        v.addSubview_(self.c["autostart_hint"])
        self.c["log_btn"] = _button("打开日志", self._r(CR - 212, cy + PAD + 2 * cbh + 1, 104, 24),
                                    self, "onLog:", small=True)
        v.addSubview_(self.c["log_btn"])
        self.c["diag_btn"] = _button("诊断…", self._r(CR - 104, cy + PAD + 2 * cbh + 1, 104, 24),
                                     self, "onDiag:", small=True)
        v.addSubview_(self.c["diag_btn"])
        cy += 3 * cbh + 2 * PAD

        # ---- 高级选项: 入口一张卡, 里面那几项不常用, 单独一个小窗 ----
        cy += gap
        self._card(v, cy, ROW_H + 2 * PAD)
        self.c["adv"] = _button("高级选项…", self._r(cx, cy + PAD + 1, 120, 26),
                                self, "onAdvanced:")
        v.addSubview_(self.c["adv"])
        self.c["adv_hint"] = _label("光标基准屏 · 未验证设备 · 判定时长",
                                    self._r(cx + 128, cy + PAD + 5, CR - cx - 128, 18),
                                    size=11, dim=True, right=True)
        v.addSubview_(self.c["adv_hint"])
        cy += ROW_H + 2 * PAD

        # 高级选项窗口: 独立小窗。内容全在主窗口这类里算 frame 会牵动整页重排,
        # 而且「高级」本来就该离常用开关远一点。
        self.adv = AdvancedWindow.alloc().initWithOwner_(self)

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
        binds = HB.binds_from_cfg(a.cfg)
        self._syncing = True
        try:
            self.c["enable"].setState_(1 if a.enabled else 0)
            self.c["debug"].setState_(1 if a.cfg["debug_log"] else 0)
            for g, _t, opts, _d in BIND_ROWS:
                self._select(self.c["bind_%s" % g], list(opts), binds.get(g))
            self._select(self.c["nat"], [k for k, _ in NAT_LABELS], a.cfg["natural"])
            from dptouch_engine import GAINS
            self._select(self.c["gain"], [g for g, _ in GAINS], a.cfg["gain"])
        finally:
            self._syncing = False
        # 滚动方向 / 速度: 只有「没有手势绑滚动」时才没意义
        # (「指定目标屏」下笔会顺便把光标带过去, 滚轮仍落在光标处 —— 不冲突, 不再禁用)
        usable = "scroll" in binds.values()
        self.c["nat"].setEnabled_(usable)
        self.c["gain"].setEnabled_(usable)
        self.c["nat_hint"].setStringValue_(
            "系统当前：%s" % ("自然" if self._sys_natural() else "传统")
            if (usable and a.cfg["natural"] == "system") else ("" if usable else "没有手势绑滚动"))
        self.c["gain_hint"].setStringValue_("" if usable else "没有手势绑滚动")
        for g, _t, _opts, _d in BIND_ROWS:
            self.c["bind_%s_hint" % g].setStringValue_(self._bind_hint(g, binds.get(g)))

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
    def _bind_hint(self, gesture, action):
        """绑定的行尾说明 —— 只在「这一行的状态容易误解」时给一句。"""
        if action == "none":
            return "关"
        if gesture == "swipe":
            return "方向与速度见下" if action == "scroll" else ""
        if gesture == "hold_swipe":
            return "先停住 %.2f 秒再划" % (int(self.app.cfg.get("hold_ms") or 250) / 1000.0)
        if gesture == "hold":
            ms = int(self.app.cfg.get("rc_hold_ms") or 0)
            return "停住 %.1f 秒不动" % (ms / 1000.0) if ms else "判定时长在高级选项"
        return ""

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
        # 末尾那 30pt 是「立即检查更新」按钮这一行 (和提示文字同一行)
        h = len(ADV_SWITCHES) * CB_H + len(ADV_POPUPS) * ROW_H + 30 + 2 * PAD
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
        self.c["hint"] = _label("改完立即生效", self._r(cx + 136, by + 5, cw - 136, 16),
                                size=11, dim=True)
        v.addSubview_(self.c["hint"])
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
