#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mipad DP Touch —— 菜单栏应用

把小米焦点触控笔 Pro 变成 macOS 上真正可用的输入设备：
笔尖轻点 = 点击，笔尖划动 = 滚动翻页（默认）或拖拽选择。

为什么需要它
------------
小米平板 9 Pro Max 用 DP-in 模式当 Mac 外接屏时，平板的触摸/笔是走独立 USB HID
接口上报的（DP 协议本身只带视频和音频）。macOS 会把笔的绝对坐标映射成光标位置，
但**不映射 TipSwitch**，于是笔能移光标却点不动；按下之后的移动也不会被合成
LeftMouseDragged，所以拖拽全部失效。本程序补齐这部分。

手指触控在 Mac 上无解（平板固件只对 Windows 暴露触摸屏接口），详见 README §2.1。

用法
----
这是菜单栏应用（不占 Dock）。点菜单栏上的图标切换模式、调方向、看连接状态。
首次运行会引导开启两项系统权限。
"""

import json
import os
import shutil
import shlex
import subprocess
import sys
import threading
import time

import objc
from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular, NSMenu, NSMenuItem,
                    NSStatusBar, NSVariableStatusItemLength, NSWorkspace, NSBundle,
                    NSBitmapImageFileTypePNG, NSGraphicsContext, NSBezierPath,
                    NSBitmapImageRep, NSImage, NSDeviceRGBColorSpace,
                    NSCompositingOperationSourceOver)
from Foundation import (NSObject, NSTimer, NSRunLoop, NSDate, NSMakeRect,
                        NSMakeSize, NSMakePoint, NSZeroRect)

import dptouch_autostart as AS
import dptouch_display as DP
import dptouch_engine as E
import dptouch_window as UI

APP_NAME = "Mipad DP Touch"
APP_VERSION = "1.0.0"
CFG_DIR = os.path.expanduser("~/Library/Application Support/MipadDPTouch")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/MipadDPTouch.log")

# 改名前 (Xiaomi DP Touch) 的路径 —— 首次启动自动搬一次, 免得老用户的设置/日志丢
LEGACY_CFG_PATH = os.path.expanduser("~/Library/Application Support/XiaomiDPTouch/config.json")
LEGACY_LOG_PATH = os.path.expanduser("~/Library/Logs/XiaomiDPTouch.log")

DEFAULTS = {
    "mode": "scroll",
    "natural": "system",
    "gain": 1.0,
    "allow_unknown": False,
    "takeover": False,
    # 触屏模式下的长按拖拽: 笔尖先停住再划 = 拖拽 (否则快速划动一律被当成滚动)
    "hold_drag": True,
    "hold_ms": 250,
    # 长按不动 = 右键菜单 (0 = 关); 笔侧键 / 橡皮擦端 = 右键
    "rc_hold_ms": 800,
    "rc_barrel": False,          # (旧) 布尔开关; 已迁到下面两条绑定
    # ★按键绑定栏: 笔侧键 / 橡皮擦端 -> 一个动作 (none/right/middle/left/double/space/back)
    "bind_barrel": "right",
    "bind_eraser": "none",
    "enabled": True,
    # 开机自启 / 详细日志 (窗口里可切)
    "autostart": False,
    "debug_log": False,
    # 显示缩放 (HiDPI): 允许管没实测验证过的屏 / 上一次切换前的档位 (一键退回用)
    "display_allow_unknown": False,
    "display_saved": None,
    # 首启引导: 权限不齐时自动打开设置窗口 —— 只做一次(开机自启每次登录都弹会烦)
    "onboarded": False,
}

GAINS = E.GAINS                  # 唯一出处; 设置窗口的「滚动速度」下拉用同一份

AX_PREFS_URLS = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
]
INPUT_PREFS_URLS = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent",
]
# 系统设置 -> 显示器。面板原生那档 (1x 3408x2272) 不在 CG API 的枚举里, 只能在这里选。
DISPLAY_PREFS_URLS = [
    "x-apple.systempreferences:com.apple.Displays-Settings.extension",
]


# --------------------------------------------------------------------------
# 配置持久化
# --------------------------------------------------------------------------

def _migrate_legacy_paths():
    """改名前 (Xiaomi DP Touch) 的配置与日志搬一次; 新路径已有则什么都不做。"""
    try:
        if not os.path.exists(CFG_PATH) and os.path.exists(LEGACY_CFG_PATH):
            os.makedirs(CFG_DIR, exist_ok=True)
            shutil.copy2(LEGACY_CFG_PATH, CFG_PATH)
    except Exception as e:
        log("搬老配置失败 (不影响使用): %r" % (e,))
    try:
        if not os.path.exists(LOG_PATH) and os.path.exists(LEGACY_LOG_PATH):
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            shutil.copy2(LEGACY_LOG_PATH, LOG_PATH)
    except Exception as e:
        log("搬老日志失败 (不影响使用): %r" % (e,))


def load_cfg():
    cfg = dict(DEFAULTS)
    _migrate_legacy_paths()
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # 旧配置只有「笔侧键 = 右键」这一个开关; 老用户升级后让它等价落到绑定上
    if cfg.pop("rc_barrel", False):
        cfg["bind_barrel"] = "right"
        cfg["bind_eraser"] = "right"
    return cfg


def save_cfg(cfg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception as e:
        log("保存配置失败: %r" % (e,))


_LOG_F = [None]
_LOG_LK = threading.Lock()


def log(msg):
    """同时写日志文件和 (如果是终端启动) 标准输出"""
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        with _LOG_LK:                      # HID 线程也会写日志, 加锁免得把行写花
            if _LOG_F[0] is None:
                os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
                _LOG_F[0] = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
            _LOG_F[0].write(line + "\n")
    except Exception:
        pass
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(line)
        sys.stdout.flush()


def fanout_output():
    """把 C 层/Python 的 stdout+stderr 都导向日志文件, 免得 .app 里看不到"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        sys.__stdout__ = f
        sys.__stderr__ = f
    except Exception:
        pass


def open_prefs(urls):
    for u in urls:
        try:
            rc = subprocess.call(["/usr/bin/open", u],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if rc == 0:
                return True
        except Exception:
            pass
    try:
        subprocess.call(["/usr/bin/open", "-b", "com.apple.systempreferences"])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 无头自检: 打印真实菜单树 (打包后的 app 也能验, 不需要点 GUI)
# --------------------------------------------------------------------------

def _dump_menu(menu, depth=0):
    out = []
    for it in menu.itemArray():
        if it.isSeparatorItem():
            out.append("  " * depth + "----")
            continue
        mark = "✓ " if it.state() else ("- " if it.isEnabled() else "(灰) ")
        out.append("  " * depth + mark + str(it.title()).replace("\n", " "))
        sub = it.submenu()
        if sub is not None:
            out += _dump_menu(sub, depth + 1)
    return out


# --------------------------------------------------------------------------
# 菜单栏应用
# --------------------------------------------------------------------------

class DPApp(NSObject):

    # ---------------- 生命周期 ----------------

    def init(self):
        self = objc.super(DPApp, self).init()
        if self is None:
            return None
        self.cfg = load_cfg()
        self.engine = E.Engine(log=log)
        # ★整份配置都要推过去。老写法只推 5 个键, 于是「长按时间 / 长按不动 / 笔侧键 = 右键」
        #   这些每次启动都被引擎自己的默认值悄悄盖掉: 设置窗口里显示 0.6 秒, 背后实际跑 0.8 秒。
        self.engine.update(**dict(self.cfg))
        self.enabled = bool(self.cfg.get("enabled", True))
        self._build_status_item()
        self._build_menu()
        # 设置窗口: 菜单栏应用也有一个看得见的主界面(状态 / 分步权限引导 / 全部选项)
        self.win = UI.SettingsWindow.alloc().initWithApp_appName_version_(
            self, APP_NAME, APP_VERSION)
        self._boot()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "tick:", None, True)
        return self

    @objc.python_method
    def _hold_instance_lock(self):
        """单实例闸门。

        开了开机自启之后「登录时自动拉起」和「自己手动打开」可能撞在一起, 两个实例
        各发一遍鼠标事件 = 点一下变两下。用 flock 钉一个锁文件; 进程死了锁自动释放,
        所以不会有「上次崩了以后再也起不来」的坑。无头自检要能和已装的 app 并存, 放行。
        """
        if os.environ.get("DPTOUCH_SELFTEST_MENU") or os.environ.get("DPTOUCH_SELFTEST_DISPLAY") \
                or os.environ.get("DPTOUCH_SELFTEST_AUTOSTART") or os.environ.get("DPTOUCH_SELFTEST_AX") \
                or os.environ.get("DPTOUCH_SELFTEST_WINDOW"):
            return True
        try:
            import fcntl
            os.makedirs(CFG_DIR, exist_ok=True)
            self._lock_f = open(os.path.join(CFG_DIR, "instance.lock"), "w")
            fcntl.flock(self._lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_f.write("%d\n" % os.getpid())
            self._lock_f.flush()
            return True
        except Exception as e:
            log("单实例闸门: %r" % (e,))
            return False

    @objc.python_method
    def _boot(self):
        log("=" * 60)
        log("%s %s 启动 (pid %d)" % (APP_NAME, APP_VERSION, os.getpid()))
        log("系统「自然滚动」= %s | 滚动方向翻转为 %s"
            % (E.system_natural_scrolling(), self.engine.b.scroll_flip))
        self._menu_open = False
        # 记下「开机这一刻」的权限: 之后权限从无到有时, 窗口要提示「重启才生效」
        # (TCC 判定是进程级的, 运行中被授权不会立刻对这个老进程生效)
        self._boot_ax = bool(self.engine.ax_trusted())
        self._boot_input = None          # engine.start() 之后才知道
        if not self._hold_instance_lock():
            log("已有另一个实例在跑, 本次退出 (两个实例会把每次点击发两遍)")
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)      # 直接退: 此刻还没开 HID 也没建状态, 不需要走 AppKit 收尾
            return
        if os.environ.get("DPTOUCH_SELFTEST_AX"):
            # 无头自检: 权限弹窗这条路径只能在打包后的 app 里验(助手进程没有 TCC 权限,
            # 点不到 GUI 按钮)。用 prompt=False, 不弹窗、可重复跑。
            import hid_bridge as _H
            log("自检 ax_request_permission(False) -> %r"
                % _H.ax_request_permission(False))
            if not (os.environ.get("DPTOUCH_SELFTEST_AUTOSTART")
                    or os.environ.get("DPTOUCH_SELFTEST_MENU")):
                sys.stdout.flush()
                NSApplication.sharedApplication().terminate_(None)   # 别继续走: 会开 HID 抢已装的 app
                return
        _sw = os.environ.get("DPTOUCH_SELFTEST_WINDOW")
        if _sw:
            # 无头自检: 设置窗口的**布局质检**。助手看不到像素, 只能把每个控件的
            # frame 和它自己那句文字用 AppKit 实测的宽度比一遍:
            # 越界 / 重叠 / 文字被切都会报出来。三条权限状态各渲一次。
            def _p(s):
                print(s, flush=True)          # 崩溃时没 flush 的 stdout 会整段丢

            def _fake_st(ax, running, rc, devs=()):
                return {"running": running, "rc": rc, "ax": ax, "devices": list(devs),
                        "events": 1234, "fwd": 0, "scroll": 34, "click": 12, "drag": 5,
                        "down": 0, "mode": self.cfg["mode"], "flip": False, "error": ""}

            _dev = (("小米平板 9 Pro Max", 0x2717, 0x2D05, True),)
            for _tag, _st_ in (("权限不齐", _fake_st(False, False, -536870174)),
                               ("只缺输入监控", _fake_st(True, False, -536870174)),
                               ("权限就绪", _fake_st(True, True, 0, _dev))):
                _p("=== 设置窗口: %s ===" % _tag)
                self.win.refresh(_st_, force=True)
                UI.dump_layout(self.win.win.contentView())
                _probs = UI.check_layout(self.win.win.contentView())
                _p("布局检查: %s" % ("全部通过" if not _probs else "%d 处问题" % len(_probs)))
                for _pr in _probs:
                    _p("  !! %s" % _pr)
                _p("头部状态行: %r" % self.win.c["conn"].stringValue())
                _p("计数行:     %r" % self.win.c["counters"].stringValue())
                _p("权限摘要:   %r" % self.win.c["perm_sum"].stringValue())
                _p("步骤1 按钮: %r enabled=%s" % (self.win.c["p_ax_btn"].title(),
                                                  bool(self.win.c["p_ax_btn"].isEnabled())))
                _p("步骤2 按钮: %r enabled=%s" % (self.win.c["p_input_btn"].title(),
                                                  bool(self.win.c["p_input_btn"].isEnabled())))
                _p("重启提示:   %r visible=%s" % (self.win.c["hint"].stringValue(),
                                                  bool(self.win.c["relaunch"].isHidden()
                                                       is False)))
            _p("=== 权限分步模型 ===")
            for _ax, _in in ((False, False), (True, False), (True, True)):
                _p("ax=%-5s 输入监控=%-5s -> %s | %s"
                   % (_ax, _in, UI.perm_summary(_ax, _in)[0],
                      " / ".join("%s=%s" % (s["title"], s["state"])
                                 for s in UI.perm_steps(_ax, _in))))
            _p("=== 「重启才生效」判定 (开机时 ax=%s input=%s) ==="
               % (self._boot_ax, self._boot_input))
            for _now in ((False, False), (True, False), (True, True)):
                _p("  现在 %s -> %s" % (_now, UI.needs_relaunch(
                    self._boot_ax, self._boot_input, *_now)))
            # --- 首启引导自检: 用户吐槽"初始化的权限索取有点暴力" ---
            # 要证明四件事: (1) 只打开我们自己的窗口; (2) 绝不自己弹系统设置/系统授权框;
            #              (3) 只引导一次 —— 开了开机自启以后每次登录都弹窗会烦人;
            #              (4) 自检过程不写脏真配置。
            _calls = []
            _noguide = os.environ.pop("DPTOUCH_NO_GUIDE", None)   # 自检要真走引导逻辑
            _open_real, _save_real = self.open_settings, globals()["save_cfg"]
            _prefs_real = globals()["open_prefs"]
            _onb_real = self.cfg.get("onboarded")
            self.open_settings = lambda: _calls.append("窗口")
            globals()["save_cfg"] = lambda cfg: None          # 别真写进用户配置
            globals()["open_prefs"] = \
                lambda urls: _calls.append("系统设置:%s" % (urls[0][:30],))
            try:
                self.cfg["onboarded"] = False
                self._first_run_guide(-536870174)
                _first = list(_calls)
                _calls = []
                self._first_run_guide(-536870174)             # 第二次: 不该再弹
                _second = list(_calls)
                _calls = []
                self.cfg["onboarded"] = True
                self._first_run_guide(0)                      # 权限齐了: 什么都不做
                _third = list(_calls)
            finally:
                self.open_settings, globals()["save_cfg"] = _open_real, _save_real
                globals()["open_prefs"] = _prefs_real
                self.cfg["onboarded"] = _onb_real
                if _noguide is not None:
                    os.environ["DPTOUCH_NO_GUIDE"] = _noguide
            _p("=== 首启引导 ===")
            _p("  第一次(权限不齐) -> 动作 %s" % (_first or ["无"]))
            _p("  第二次(同一会话) -> 动作 %s" % (_second or ["无"]))
            _p("  权限已齐         -> 动作 %s" % (_third or ["无"]))
            _p("  自检: %s" % ("通过" if (_first == ["窗口"] and not _second
                                         and not _third) else "不通过"))
            _p("=== 重启命令 (「立即重启」会跑的那条) ===")
            _p("  %s" % (self.relaunch_cmd() or "(空)"))
            _p("=== 界面文案里的括号 (用户嫌多) ===")
            _bad = []
            for _k, _v in self.win.c.items():
                try:
                    _t = _v.title() if _v.__class__.__name__ == "NSButton" \
                        else (_v.stringValue() or "")
                except Exception:
                    _t = ""
                if "（" in _t or "(" in _t:
                    _bad.append("%s=%s" % (_k, _t))
            _p("  " + ("；".join(sorted(_bad)) if _bad else "一处都没有"))
            if _sw == "exit" or _sw == "show" or _sw.startswith("shot"):
                if _sw != "exit":
                    # 真把窗口显示出来一次 (证明"界面能弹出来"), 读到状态就立刻收起
                    self.win.show()
                    _p("显示自检: isVisible=%s frame=%s"
                       % (bool(self.win.win.isVisible()), self.win.win.frame()))
                    if _sw.startswith("shot"):
                        # 把窗口内容离屏渲染成 PNG (给 README/文档配图用)。
                        # 走 AppKit 自己渲染, 所以要不了「屏幕录制」权限, 也不会
                        # 拍到桌面背景 —— 拍出来就是这一个窗口本身。
                        _path = _sw.split(":", 1)[1]
                        # 不激活的话红黄绿三个按钮是灰的, 出图看着像"窗口没被选中"
                        try:
                            self.win.win.makeKeyAndOrderFront_(None)
                            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                        except Exception:
                            pass
                        NSRunLoop.currentRunLoop().runUntilDate_(
                            NSDate.dateWithTimeIntervalSinceNow_(0.8))
                        _cv = self.win.win.contentView()
                        # ★给 README 出图: 连标题栏一起拍, 看起来才像"一个真窗口",
                        # 而不是飘在灰底上的一块内容。主题框 (contentView 的父视图) 自己会
                        # 画标题栏和红绿灯; 拿不到就退回只拍内容视图。
                        _fr = _cv.superview() or _cv
                        _rec_cv = _cv.bounds()
                        _rec = _fr.bounds()
                        # (a) 先把背景铺一层窗口背景色 —— 视图自己不画背景,
                        #     不然渲染出来底色是透明的 (看起来像黑底)
                        _rep = _fr.bitmapImageRepForCachingDisplayInRect_(_rec)
                        _bgc = self.win.win.backgroundColor()
                        if _bgc is not None:
                            _g = NSGraphicsContext.graphicsContextWithBitmapImageRep_(_rep)
                            NSGraphicsContext.saveGraphicsState()
                            NSGraphicsContext.setCurrentContext_(_g)
                            _bgc.setFill()
                            # 用像素尺寸铺满 (这个 context 的坐标系是像素)
                            NSBezierPath.fillRect_(
                                NSMakeRect(0, 0, _rep.pixelsWide(), _rep.pixelsHigh()))
                            NSGraphicsContext.restoreGraphicsState()
                        _fr.cacheDisplayInRect_toBitmapImageRep_(_rec, _rep)
                        # (a2) 窗口在屏幕上本来是圆角的, 离屏 cacheDisplay 出来是方的 ->
                        #      这里按系统半径把四角裁掉, README 配图才不像"贴在灰底上的方块"
                        try:
                            _pw, _ph = _rep.pixelsWide(), _rep.pixelsHigh()
                            _round = NSBitmapImageRep.alloc(
                            ).initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                                None, _pw, _ph, 8, 4, True, False,
                                NSDeviceRGBColorSpace, 0, 0)
                            _gc = NSGraphicsContext.graphicsContextWithBitmapImageRep_(_round)
                            NSGraphicsContext.saveGraphicsState()
                            NSGraphicsContext.setCurrentContext_(_gc)
                            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                                NSMakeRect(0, 0, _pw, _ph), 20.0, 20.0).addClip()
                            _srci = NSImage.alloc().initWithSize_(NSMakeSize(_pw, _ph))
                            _srci.addRepresentation_(_rep)
                            _srci.drawAtPoint_fromRect_operation_fraction_(
                                NSMakePoint(0, 0), NSZeroRect,
                                NSCompositingOperationSourceOver, 1.0)
                            NSGraphicsContext.restoreGraphicsState()
                            _rep = _round
                        except Exception as _e:
                            _p("圆角裁切: 跳过 (%s)" % _e)
                        _dat = _rep.representationUsingType_properties_(
                            NSBitmapImageFileTypePNG, {})
                        _ok = bool(_dat.writeToFile_atomically_(_path, True))
                        _p("截图自检: ok=%s -> %s | %d×%d | %d 字节"
                           % (_ok, _path, _rep.pixelsWide(), _rep.pixelsHigh(),
                              len(_dat)))
                        # (b) 同一时刻再导一份 PDF (矢量, 文字必在), 便于比对/放大
                        try:
                            _pdf = self.win.win.dataWithPDFInsideRect_(_rec_cv)
                            _pok = bool(_pdf.writeToFile_atomically_(_path + ".pdf", True))
                            _p("PDF 自检: ok=%s -> %s.pdf | %d 字节"
                               % (_pok, _path, len(_pdf)))
                        except Exception as _e:
                            _p("PDF 自检: 失败 %s" % _e)
                    self.win.win.orderOut_(None)
                NSApplication.sharedApplication().terminate_(None)
                return
        _sa = os.environ.get("DPTOUCH_SELFTEST_AUTOSTART")
        if _sa:
            # 无头自检: 真机开/关一次开机自启, 打印系统实际状态 (菜单那条路等价)
            if _sa == "on":
                _ok, _m, _b = AS.enable()
                print("自启 on  -> ok=%s backend=%s | %s" % (_ok, _b, _m))
            elif _sa == "off":
                _ok, _m = AS.disable()
                print("自启 off -> ok=%s | %s" % (_ok, _m))
            print("自启 status: %s" % (AS.status(),))
            print("自启 status_line: %s" % AS.status_line())
            sys.stdout.flush()
            NSApplication.sharedApplication().terminate_(None)
            return
        _st = os.environ.get("DPTOUCH_SELFTEST_MENU")
        if _st:
            # 无头自检: 打印真实菜单树 + 显示器现状 (含「显示缩放」这组)
            print("=== 菜单树 ===")
            for _l in _dump_menu(self.item.menu()):
                print(_l)
            tgt, why, others = DP.summary()
            print("=== 显示器 ===")
            print("目标屏: %s | %s" % (tgt and ("%s (id=%d, %s)" % (tgt["name"], tgt["id"],
                                                                   tgt["match"])), why))
            print("其它屏 %d 块: %s" % (len(others), [d["name"] for d in others] or "无"))
            if tgt is not None:
                print("HiDPI 档: %s" % [DP.label(m, DP.panel_native(tgt))
                                        for m in DP.options(tgt["id"],
                                                            panel=DP.panel_native(tgt))])
            sys.stdout.flush()
            if _st == "exit":
                # 自检到此为止: 不再打开桥接 (免得和已装的 app 抢 HID / 弹权限窗)
                NSApplication.sharedApplication().terminate_(None)
                return
        _sd = os.environ.get("DPTOUCH_SELFTEST_DISPLAY")
        if _sd:
            # 无头自检: 真的走一遍菜单动作 (找菜单项 -> actDisplayScale_ -> 读回校验 -> 再切回)
            def _item_for(wh):
                for i, o in enumerate(self._disp_opts):
                    if (o["w"], o["h"]) == wh:
                        for it in self.mi_disp:
                            if it.tag() == i and it.action() is not None:
                                return it
                return None

            print("=== 显示缩放: 子菜单重建 x2 (覆盖 removeItem+重建 这条路径) ===")
            for _k in (1, 2):
                self._sync_display(force=True)
                print("  第 %d 次重建 ok: 子菜单项数 = %d" % (_k, self.mg_disp.numberOfItems()))
            print("=== 显示缩放: 走菜单动作 (不是直接调模块) ===")
            tgt = self._disp_tgt
            print("目标屏: %s" % (tgt and tgt["name"]))
            for wh in [(1280, 853), (1704, 1136)]:
                it = _item_for(wh)
                if it is None:
                    print("菜单里没有 %s -> 跳过" % (wh,))
                    continue
                print("点菜单项: %s" % it.title())
                self.actDisplayScale_(it)
                cur = DP.current(tgt["id"])
                self._sync_display(force=True)
                it2 = _item_for(wh)
                print("  -> 现在 UI %dx%d / fb %dx%d | 期望 %s -> %s | 菜单打勾 = %s"
                      % (cur["w"], cur["h"], cur["pw"], cur["ph"], wh,
                         (cur["w"], cur["h"]) == wh, bool(it2 is not None and it2.state())))
            print("存档 display_saved = %s" % (self.cfg.get("display_saved"),))
            sys.stdout.flush()
            if _sd == "exit":
                NSApplication.sharedApplication().terminate_(None)
                return
        if not self.enabled:
            log("配置为「不自动启用」, 桥接未打开")
            return
        rc = self.engine.start()
        self._boot_input = (rc == 0)
        log("IOHIDManagerOpen rc = %d (%s)" % (rc, "成功" if rc == 0 else "失败"))
        self.engine.set_debug(bool(self.cfg["debug_log"]))
        if rc == 0:
            self.engine.log("桥接已开启")
        self._first_run_guide(rc)

    @objc.python_method
    def _first_run_guide(self, rc):
        """权限不齐时**打开设置窗口**(引导在里面分步走)。

        这里只做两件事, 都不替用户做动作:
        * 不再一上来弹一整面文字 + 直接把系统设置面板拉起来 (那才是"暴力");
        * 不代用户去点系统设置 —— 「打开设置」那一下由用户自己在窗口里点。
        另外只在第一次这么做: 开了开机自启以后每次登录都弹窗会烦死人。
        """
        if os.environ.get("DPTOUCH_NO_GUIDE"):
            log("已跳过权限引导 (DPTOUCH_NO_GUIDE)")
            return
        miss = [s["title"] for s in UI.perm_steps(self.engine.ax_trusted(), rc == 0)
                if not s["ok"]]
        if not miss:
            return
        log("权限不齐 (%s) -> 打开设置窗口分步引导" % "、".join(miss))
        if self.cfg.get("onboarded"):
            log("(首启引导做过一次了, 不再自动弹窗; 菜单里有「完成授权…」)")
            return
        self.cfg["onboarded"] = True
        save_cfg(self.cfg)
        self.open_settings()

    # ---------------- 菜单 ----------------

    @objc.python_method
    def _build_status_item(self):
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.button().setTitle_("\u270e")     # ✎
        self.item.button().setToolTip_(APP_NAME)

    @objc.python_method
    def _mk(self, menu, title, action=None, key="", tag=0, check=False):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            it.setTarget_(self)
        else:
            it.setEnabled_(False)
        it.setTag_(tag)
        if check:
            it.setState_(1)
        menu.addItem_(it)
        return it

    @objc.python_method
    def _sep(self, menu):
        """分隔符 —— 必须真的 addItem_ 进菜单, 否则之后 removeItem_ 会抛
        NSInternalInconsistencyException (而且菜单里根本没显示出来)。"""
        it = NSMenuItem.separatorItem()
        menu.addItem_(it)
        return it

    @objc.python_method
    def _sub(self, menu, title):
        """建一个子菜单, 返回**父菜单项** —— 改标题要改它 (改 NSMenu.title 不会同步)"""
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        sm = NSMenu.alloc().initWithTitle_(title)
        sm.setAutoenablesItems_(False)
        it.setSubmenu_(sm)
        menu.addItem_(it)
        return it

    @objc.python_method
    def _build_menu(self):
        """菜单只留**常用开关**。

        设置项、权限说明、显示器档位全都搬到设置窗口 —— 菜单栏应用的正规做法是
        「菜单短、窗口全」。以前 20 多项、每项后面还挂一句括号解释, 自然不像正常软件。
        """
        m = NSMenu.alloc().init()
        m.setAutoenablesItems_(False)
        self.mi_status = self._mk(m, "正在启动…")
        self.mi_grant = self._mk(m, "完成授权…", "actOpenWindow:")   # 权限不齐时才露面
        self.mi_enable = self._mk(m, "启用笔桥接", "actToggleEnable:", check=self.enabled)
        self.mi_counters = self._mk(m, "清零计数", "actClearCounters:")
        self._sep(m)

        # --- 显示缩放: 高频动作, 菜单里留一份 (档位与窗口是同一份) ---
        self.mi_disp_parent = self._sub(m, "显示缩放")
        self.mg_disp = self.mi_disp_parent.submenu()
        self.mi_disp = []        # 动态项, 每次扫描整组重建
        self._disp_opts = []     # 当前列出的档位 (tag -> 下标)
        self._disp_tgt = None
        self._disp_tick = 0
        self._sep(m)

        self._mk(m, "设置…", "actOpenWindow:", key=",")
        self._mk(m, "打开日志", "actOpenLog:")
        self._sep(m)
        self._mk(m, "退出 %s" % APP_NAME, "actQuit:", key="q")

        self.item.setMenu_(m)
        m.setDelegate_(self)      # 菜单显示期间不许改它 (见 menuWillOpen_ / tick_)
        self._reflect()
        self._sync_display(force=True)

    @objc.python_method
    def _reflect(self):
        """状态 -> 菜单。**只动菜单里真有的项** —— 设置项都在窗口里, 窗口自己刷。"""
        self.mi_enable.setState_(1 if self.enabled else 0)
        st = self.engine.status()
        miss = [s["title"] for s in UI.perm_steps(
            bool(st["ax"]), bool(st["running"]) and st["rc"] == 0) if not s["ok"]]
        self.mi_grant.setHidden_(not miss)

    @objc.python_method
    def _apply(self, **kw):
        self.cfg.update(kw)
        self.engine.update(**kw)
        save_cfg(self.cfg)
        self._reflect()

    # ---------------- 定时刷新 ----------------

    def tick_(self, timer):
        """每秒刷新。

        菜单显示期间**一个菜单项都不动** —— AppKit 不允许改正在显示的菜单; 在 Tahoe 的
        新状态栏 scene 架构下这么干会把菜单会话卡住, 之后所有点击 (包括我们合成的点击)
        都被那个半死的会话吃掉, 表现就是「点开菜单栏图标之后, 笔再点就没反应」。
        这一拍跳过, 下一拍 (≤1 秒) 会补上。
        """
        if getattr(self, "_menu_open", False):
            return
        try:
            self._tick_body()
            self._tick_err = ""
        except Exception as e:
            # 定时器回调里出错绝不能静默 (分隔符那次就是被吞掉才查了半天)
            msg = repr(e)
            if msg != getattr(self, "_tick_err", ""):
                self._tick_err = msg
                log("刷新异常: %s" % msg)

    @objc.python_method
    def _tick_body(self):
        st = self.engine.status()
        self.mi_status.setTitle_(self.engine.status_line_short())
        self._reflect()
        self.win.refresh(st)         # 窗口没开时它自己会立刻返回
        self._sync_display()

    @objc.python_method
    def _sync_display(self, force=False):
        """重建「显示缩放」子菜单 (每 10 秒自动一次, 或点「重新扫描」立即)。

        菜单里只留**档位本身**。目标屏 / 帧缓冲 / 各种说明文字都在设置窗口里 ——
        菜单栏应用的菜单要短, 挂一堆括号解释就不像个正常软件了。
        红线条: 只操作平板那块屏。认不出/认出多块 -> 整组只显示原因, 一个动作都不给。
        """
        if getattr(self, "_menu_open", False) and not force:
            return            # 菜单正显示着: 不许改它 (见 tick_ 的说明)
        self._disp_tick += 1
        if not force and (self._disp_tick - 1) % 10 != 0:
            return
        for it in self.mi_disp:
            if it.menu() is self.mg_disp:        # 不在本菜单里就跳过: NSMenu 会抛异常
                self.mg_disp.removeItem_(it)
        self.mi_disp = []
        self._disp_opts = []
        tgt, why, _others = DP.summary()
        self._disp_tgt = tgt
        self.mi_disp_parent.setTitle_("显示缩放")
        if tgt is None:
            self.mi_disp.append(self._mk(self.mg_disp, why))
        else:
            panel = DP.panel_native(tgt)
            cur = DP.current(tgt["id"])
            for m in DP.options(tgt["id"], panel=panel):
                self._disp_opts.append(m)
                self.mi_disp.append(self._mk(
                    self.mg_disp, "%d×%d" % (m["w"], m["h"]), "actDisplayScale:",
                    tag=len(self._disp_opts) - 1,
                    check=(m["w"], m["h"]) == (cur["w"], cur["h"])))
            if not self._disp_opts:
                self.mi_disp.append(self._mk(self.mg_disp, "这块屏没有可选档位"))
        self.mi_disp.append(NSMenuItem.separatorItem())
        it = self._mk(self.mg_disp, "退回切换前的档位", "actDisplayRestore_")
        if not self.cfg.get("display_saved"):
            it.setEnabled_(False)
        self.mi_disp.append(it)
        self.mi_disp.append(NSMenuItem.separatorItem())
        self.mi_disp.append(self._mk(self.mg_disp, "重新扫描显示器",
                                     "actDisplayRescan_"))
        self.mi_disp.append(self._mk(self.mg_disp, "打开「显示器」设置…",
                                     "actOpenDisplaySettings_"))

    # ---------------- 动作 ----------------

    def actToggleEnable_(self, sender):
        self.enabled = not self.enabled
        if self.enabled:
            rc = self.engine.start()
            if rc != 0:
                self._first_run_guide(rc)
        else:
            self.engine.stop()
        self.cfg["enabled"] = self.enabled
        save_cfg(self.cfg)
        self._reflect()

    def actClearCounters_(self, sender):
        self.engine.zero_counters()      # 连 Bridge 的计数一起清 (HID 线程在动那批字段)
        log("计数已清零")

    def actMode_(self, sender):
        self.set_mode(str(sender.representedObject()))

    def actNatural_(self, sender):
        self.set_natural(str(sender.representedObject()))

    def actGain_(self, sender):
        self.set_gain(sender.representedObject())

    def actUnknown_(self, sender):
        self._apply(allow_unknown=not self.cfg["allow_unknown"])

    def actDispAllow_(self, sender):
        """允许管理未实测的屏幕 (默认关: 多屏环境里认错屏的代价太大)"""
        self._apply(display_allow_unknown=not self.cfg["display_allow_unknown"])
        self._sync_display(force=True)

    def actTakeover_(self, sender):
        self._apply(takeover=not self.cfg["takeover"])

    def actHold_(self, sender):
        self._apply(hold_drag=not bool(self.cfg["hold_drag"]))

    # ---------------- 开机自启 / 详细日志 ----------------

    def actAutostart_(self, sender):
        want = not bool(self.cfg["autostart"])
        ok, msg = AS.set_enabled(want)
        self.cfg["autostart"] = bool(want and ok)
        save_cfg(self.cfg)
        log("开机自启 -> %s: %s" % ("开" if want else "关", msg))
        self._reflect()
        if not ok:
            self._warn("开机自启没设置成功", "%s\n\n(已记进日志: %s)" % (msg, LOG_PATH))
        elif want and "批准" in msg:
            AS.open_login_items()      # 需要用户去系统设置里点一下, 直接把他送过去

    def actDebugLog_(self, sender):
        self._apply(debug_log=not bool(self.cfg["debug_log"]))
        self.engine.set_debug(self.cfg["debug_log"])
        log("详细日志 -> %s" % ("开" if self.cfg["debug_log"] else "关"))

    # ---------------- 菜单开关护栏 ----------------

    def menuWillOpen_(self, menu):
        """菜单开始显示: 标记住, 期间不碰任何菜单项 (见 tick_)。"""
        self._menu_open = True
        log("菜单已打开")

    def menuDidClose_(self, menu):
        self._menu_open = False
        # 菜单开着这段时间攒下的半按 / 划动残留丢掉, 免得带进下一次触摸
        try:
            self.engine.reset_transient()
        except Exception as e:
            log("菜单关闭复位失败: %r" % (e,))
        log("菜单已关闭")

    # ---------------- 显示缩放 ----------------

    @objc.python_method
    def _warn(self, title, body):
        if os.environ.get("DPTOUCH_NO_ALERT"):
            log("!! %s: %s" % (title, body))     # 无头模式: 只记日志, 不弹窗
            return
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(body)
        a.addButtonWithTitle_("好")
        a.runModal()

    @objc.python_method
    def _remember_prev(self, tgt, prev):
        """记下切换前那档, 让「退回」变成一键来回 (并且绑身份, 插拔后不会张冠李戴)"""
        if not prev:
            return
        self._apply(display_saved={"vendor": tgt["vendor"], "model": tgt["model"],
                                   "name": tgt["name"], "w": prev["w"], "h": prev["h"],
                                   "hz": prev["hz"], "hidpi": prev["hidpi"]})

    @objc.python_method
    def _after_display_change(self):
        """换尺寸后绝对坐标的归一化基准就变了 -> 重建 Bridge (不碰 IOHIDManager, 笔不中断)"""
        try:
            w, h = self.engine.resync_display()
            log("坐标范围已按 UI %dx%d 刷新" % (w, h))
        except Exception as e:
            log("刷新坐标范围失败: %r" % (e,))
        self._sync_display(force=True)

    def actDisplayScale_(self, sender):
        self.apply_display_index(int(sender.tag()))

    @objc.python_method
    def apply_display_index(self, i):
        """切到「显示缩放」列表里的第 i 档 —— 菜单与窗口共用这一条路径。

        返回 True = 已经在这一档(或切换成功)。已经在的那档直接返回: 窗口的下拉每次
        重建都会重新选一次, 不能因为这个平白跑一遍显示器配置事务。
        """
        tgt = self._disp_tgt
        if tgt is None or not (0 <= i < len(self._disp_opts)):
            return False
        m = self._disp_opts[i]
        panel = DP.panel_native(tgt)
        cur = DP.current(tgt["id"])
        if (m["w"], m["h"]) == (cur["w"], cur["h"]):
            log("显示缩放: 已经是 %s, 不动" % DP.label(m, panel))
            return True
        r = DP.apply(tgt["id"], m["ref"], m["w"], m["h"], m["hz"],
                     allow_unverified=self.cfg["display_allow_unknown"])
        log("显示缩放 -> %s | %s" % (DP.label(m, panel), r["msg"]))
        if not r["ok"]:
            self._warn("切换失败", r["msg"])
            return False
        self._remember_prev(tgt, r.get("prev"))
        self._after_display_change()
        return True

    def actDisplayRestore_(self, sender):
        saved = self.cfg.get("display_saved")
        tgt = self._disp_tgt
        if not saved or tgt is None:
            return
        if (saved.get("vendor"), saved.get("model")) != (tgt["vendor"], tgt["model"]):
            self._warn("找不到那块屏", "上次改的是「%s」, 现在它不在。插好平板再试。"
                       % saved.get("name", "?"))
            return
        m = DP.find_mode(tgt["id"], saved["w"], saved["h"], hz=saved.get("hz"),
                         hidpi=saved.get("hidpi"))
        if m is None:
            self._warn("这一档系统不通过 API 给",
                       "UI %d×%d 不在 CGDisplayCopyAllDisplayModes 的枚举结果里"
                       "（面板原生 1x 档就是这样）。\n\n"
                       "请用「打开「显示器」设置…」在系统设置里选回去。"
                       % (saved["w"], saved["h"]))
            return
        panel = DP.panel_native(tgt)
        r = DP.apply(tgt["id"], m["ref"], m["w"], m["h"], m["hz"],
                     allow_unverified=self.cfg["display_allow_unknown"])
        log("显示缩放 退回 -> %s | %s" % (DP.label(m, panel), r["msg"]))
        if not r["ok"]:
            self._warn("退回失败", r["msg"])
            return
        self._remember_prev(tgt, r.get("prev"))      # 再点一次就切回去 = 一键来回
        self._after_display_change()

    def actDisplayRescan_(self, sender):
        self._sync_display(force=True)
        log("显示: 已重新扫描 (%s)"
            % (self._disp_tgt["name"] if self._disp_tgt else "未找到平板屏"))

    def actDisplayAllowUnknown_(self, sender):
        self._apply(display_allow_unknown=not self.cfg["display_allow_unknown"])
        log("显示: 允许管理未实测的屏 = %s" % self.cfg["display_allow_unknown"])
        self._sync_display(force=True)

    def actOpenDisplaySettings_(self, sender):
        open_prefs(DISPLAY_PREFS_URLS)

    # ---------------- 设置窗口打交道的入口 ----------------
    # 窗口不自己留状态: 读 app.engine / app.cfg / app.enabled, 写一律走这里的方法。

    @objc.python_method
    def log(self, msg):
        log(msg)

    @objc.python_method
    def open_settings(self):
        self.win.show()

    def actOpenWindow_(self, sender):
        self.open_settings()

    @objc.python_method
    def autostart_status(self):
        """开机自启的**系统实际状态** (用户可能在系统设置里改过, 不能只看 cfg)。"""
        try:
            return AS.status()
        except Exception as e:
            return {"enabled": False, "detail": "状态读不出来: %r" % (e,)}

    @objc.python_method
    def perm_step(self, i):
        """窗口里第 i 步的「打开设置」: 0 = 辅助功能, 1 = 输入监控。

        只有用户点这一步的时候才去打开面板 / 触发系统弹窗 —— 不代他做决定。
        """
        if int(i) == 0:
            self.actOpenAx_(None)
        else:
            self.actOpenInput_(None)

    @objc.python_method
    def set_mode(self, key):
        if key not in ("scroll", "select"):
            return
        self._apply(mode=key)
        log("模式 -> %s" % key)

    @objc.python_method
    def set_natural(self, key):
        if key not in E.NATURAL_OPTS:
            return
        self._apply(natural=key)
        log("滚动方向 -> %s" % key)

    @objc.python_method
    def set_gain(self, g):
        try:
            g = float(g)
        except Exception:
            return
        if not any(abs(g - v) < 1e-9 for v, _ in GAINS):
            return
        self._apply(gain=g)
        log("滚动速度 -> %.2gx" % g)

    @objc.python_method
    def set_hold_ms(self, ms):
        import hid_bridge as HB
        try:
            ms = int(ms)
        except Exception:
            return
        if ms not in HB.HOLD_MS_OPTS:
            return
        self._apply(hold_ms=ms)
        log("长按判定 -> %d ms" % ms)

    @objc.python_method
    def set_rc_hold(self, ms):
        """长按不动多久 = 右键菜单 (0 = 关)"""
        import hid_bridge as HB
        try:
            ms = int(ms)
        except Exception:
            return
        if ms not in HB.RC_HOLD_MS_OPTS:
            return
        self._apply(rc_hold_ms=ms)
        log("长按不动 -> %s" % ("关" if not ms else "%.1f s = 右键菜单" % (ms / 1000.0)))

    @objc.python_method
    def set_bind(self, which, key):
        """★按键绑定栏: which = 'barrel'(笔侧键) / 'eraser'(橡皮擦端), key = 动作代号"""
        import hid_bridge as HB
        key = str(key or "none")
        if key not in HB.BIND_TITLES:
            return
        field = "bind_barrel" if which == "barrel" else "bind_eraser"
        self._apply(**{field: key})
        log("%s -> %s" % ("笔侧键" if which == "barrel" else "橡皮擦端",
                          HB.BIND_TITLES[key]))

    @objc.python_method
    def relaunch_cmd(self):
        """重启用什么命令: 装在 .app 里就 open -a 那个 bundle, 开发态就直接跑脚本。

        单独抽出来是为了能被无头自检验到 —— 真跑一次会把 App 重启掉, 没法在自检里做。
        """
        try:
            bundle = NSBundle.mainBundle().bundlePath()
        except Exception:
            bundle = ""
        if bundle.endswith(".app"):
            return "/usr/bin/open -a %s" % shlex.quote(bundle)
        if sys.argv and sys.argv[0]:
            return "%s %s" % (shlex.quote(sys.executable),
                              shlex.quote(os.path.abspath(sys.argv[0])))
        return ""

    @objc.python_method
    def actRelaunch_(self, sender=None):
        """重启本程序 —— 授权后 TCC 要求新进程才生效, 这一步本来要用户自己去退出再打开。"""
        log("重启: 由设置窗口里的「立即重启」触发")
        try:
            self.engine.stop()
        except Exception:
            pass
        cmd = self.relaunch_cmd()
        if cmd:
            # 先退再起: 锁文件随进程退出释放, 睡 1 秒保证新实例能拿到锁
            subprocess.Popen(["/bin/sh", "-c", "sleep 1; " + cmd])
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)      # 硬退: 不让半死的 AppKit 会话拖住重启

    def actOpenAx_(self, sender):
        import hid_bridge as H
        open_prefs(AX_PREFS_URLS)
        H.ax_request_permission(True)

    def actOpenInput_(self, sender):
        open_prefs(INPUT_PREFS_URLS)

    def actRecheck_(self, sender):
        """授权后 macOS 要求进程重启才生效 —— 这里重建 HID 连接并刷新权限状态。"""
        rc = self.engine.restart()
        self.engine.reset_counters()
        self._reflect()
        log("重新检测: IOHIDManagerOpen rc=%d | 辅助功能=%s"
            % (rc, self.engine.status()["ax"]))
        if rc == 0:
            body = "拿笔在平板上轻点一下试试。"
        else:
            body = ("去「系统设置 → 隐私与安全性 → 输入监控」把 %s 打开, 再点一次。\n"
                    "(HID rc=%d)" % (APP_NAME, rc))
        a = NSAlert.alloc().init()
        a.setMessageText_("就绪" if rc == 0 else "还差一项权限")
        a.setInformativeText_(body)
        a.addButtonWithTitle_("好")
        a.runModal()

    def actOpenLog_(self, sender):
        if not os.path.exists(LOG_PATH):
            log("(日志刚刚创建)")
        subprocess.call(["/usr/bin/open", "-a", "Console", LOG_PATH])

    def actDiagnostics_(self, sender):
        st = self.engine.status()
        alert = NSAlert.alloc().init()
        alert.setMessageText_("诊断结果")
        lines = [
            "桥接: %s" % ("运行中" if st["running"] else "已停止"),
            "IOHIDManagerOpen rc: %d" % st["rc"],
            "辅助功能权限: %s" % ("已授权" if st["ax"] else "未授权"),
            "设备: %s" % ("、".join(d[0] for d in st["devices"]) or "无"),
            "HID 事件 %d / 转发 %d (滚动 %d, 点击 %d)" % (st["events"], st["fwd"], st["scroll"], st["click"]),
            "模式: %s | 方向翻转: %s | 增益 %.2g" % (st["mode"], st["flip"], self.cfg["gain"]),
            "系统自然滚动: %s" % E.system_natural_scrolling(),
            "",
            "日志: %s" % LOG_PATH,
        ]
        if st["error"]:
            lines.insert(0, "最近错误: %s\n" % st["error"])
        alert.setInformativeText_("\n".join(lines))
        alert.addButtonWithTitle_("好")
        alert.runModal()


    def actQuit_(self, sender):
        log("退出")
        try:
            self.engine.stop()
        except Exception:
            pass
        NSApplication.sharedApplication().terminate_(None)


def main():
    fanout_output()
    AS.set_logger(log)
    try:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        delegate = DPApp.alloc().init()
        if delegate is None:
            log("初始化失败")
            return 1
        app.run()
    except Exception:
        import traceback
        log("崩溃:\n" + traceback.format_exc())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
