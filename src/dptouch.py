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
import dptouch_update as UPD
import dptouch_window as UI
import hid_bridge as HB

APP_NAME = "Mipad DP Touch"
APP_VERSION = "1.1"            # 发版时改这里 + build_app.sh 的 VER (两处要一致)
CFG_DIR = os.path.expanduser("~/Library/Application Support/MipadDPTouch")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/MipadDPTouch.log")

# 改名前 (Xiaomi DP Touch) 的路径 —— 首次启动自动搬一次, 免得老用户的设置/日志丢
LEGACY_CFG_PATH = os.path.expanduser("~/Library/Application Support/XiaomiDPTouch/config.json")
LEGACY_LOG_PATH = os.path.expanduser("~/Library/Logs/XiaomiDPTouch.log")

DEFAULTS = {
    "natural": "system",
    "gain": 1.0,
    # 手势 -> 动作 的按键绑定。默认 = 本工具一直以来的行为, 窗口里的「恢复默认」
    # 就是把这一份写回去; 唯一出处是 hid_bridge 顶部的 BIND_GESTURES 表。
    **{HB.bind_key(g): a for g, a in HB.DEFAULT_BINDS.items()},
    "allow_unknown": False,
    # 光标基准屏: 笔的绝对坐标铺到哪块屏上。
    #   "auto"   跟随光标 —— 不接管坐标, 光标在哪块屏笔就在那块屏生效 (默认, 老行为)
    #   "tablet" 平板那块屏 —— 接管坐标, 笔能把光标带到那块屏
    #   "<id>"   指定某块屏 (菜单里选的 CGDisplayID)
    "target_display": DP.TARGET_AUTO,
    # 拖拽位置源改用笔的绝对坐标 (高级选项; 用系统光标拖不动时才需要)
    "drag_pen": False,
    # 「停住再滑」/「停住不动」的判定时长
    "hold_ms": 250,
    "rc_hold_ms": 800,
    "enabled": True,
    # 开机自启 / 详细日志 (窗口里可切)
    "autostart": False,
    "debug_log": False,
    # 显示缩放 (HiDPI): 允许管没实测验证过的屏 / 上一次切换前的档位 (一键退回用)
    "display_allow_unknown": False,
    "display_saved": None,
    # 首启引导: 权限不齐时自动打开设置窗口 —— 只做一次(开机自启每次登录都弹会烦)
    "onboarded": False,
    # 检查更新: 启动后静默查一次 + 之后每天一次。只有这一处会联网。
    #   update_skip = 用户点了「以后再说」的那一版, 之后不再自动提醒 (手动查还查得到)
    "update_auto": True,
    "update_last_check": 0,
    "update_skip": "",
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
    raw = {}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw:
        cfg.update(raw)
        # 老配置 (只有 mode / hold_drag, 还没有这四类手势的 bind_* 键) -> 翻译成等价的按键绑定。
        # 不翻译的话升级后手感会悄悄变回默认 (比如原来选的是「滑动选择」)。
        # 注意: 只看这四类手势的键 —— 更早版本还留过 bind_barrel / bind_eraser 这种死键,
        # 拿它们当"已经迁移过"会漏掉真正该做的那次翻译。
        if not any(HB.bind_key(g) in raw for g in HB.DEFAULT_BINDS):
            log("老配置迁移: 滑动方式 %r / 停住再划 %r / 停住不动 %r -> 按键绑定"
                % (raw.get("mode"), raw.get("hold_drag"), raw.get("rc_hold_ms")))
            for g, a in HB.legacy_binds(raw).items():
                cfg[HB.bind_key(g)] = a
        # 老配置的 takeover (用笔的绝对坐标驱动光标) -> 光标基准屏。
        # 当年它写死铺在系统主屏上, 接了第二块屏笔就跨不过去; 现在改成「哪块屏」可选。
        if "target_display" not in raw and "takeover" in raw:
            cfg["target_display"] = (DP.TARGET_TABLET if raw.get("takeover")
                                     else DP.TARGET_AUTO)
            log("老配置迁移: takeover %r -> 光标基准屏 %r"
                % (raw.get("takeover"), cfg["target_display"]))
        cfg["target_display"] = _clean_target(cfg.get("target_display"))
    return cfg


def _clean_target(v):
    """光标基准屏的合法值: auto / tablet / 数字 displayID。别的 (含空格、空串) 一律回 auto。"""
    v = "" if v is None else str(v).strip()
    if v in (DP.TARGET_AUTO, DP.TARGET_TABLET):
        return v
    return v if v.isdigit() else DP.TARGET_AUTO


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

def _sel(name):
    """Python 方法名 -> 真正的 ObjC 选择器。

    写错这一步的代价是**静默失效**: 菜单项照样显示、照样能点, 但 AppKit 找不到那个
    选择器, 点击被丢掉 —— 不打勾、不报错、日志一个字都没有。``def actFoo_(self, sender)``
    的选择器是 ``actFoo:``; 传 ``"actFoo_:"`` 会去找一个名叫 ``actFoo_:`` 的选择器 (不存在)。
    """
    name = str(name)
    if name.endswith("_:"):
        return name[:-2] + ":"          # actFoo_: -> actFoo:   (最常见的一种写错)
    if name.endswith("_"):
        return name[:-1] + ":"          # actFoo_  -> actFoo:
    return name


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
        # ★整份配置都要推过去。老写法只推 5 个键, 于是「长按时间 / 长按不动」
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
                or os.environ.get("DPTOUCH_SELFTEST_TARGET") \
                or os.environ.get("DPTOUCH_SELFTEST_SCREEN") \
                or os.environ.get("DPTOUCH_SELFTEST_UPDATE") \
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
        # 检查更新: 网络在后台线程, 主线程每秒那一拍取结果 (从别的线程碰 AppKit 是雷)
        self._upd_in = None              # 后台线程放结果的信箱: (kind, data)
        self._upd_busy = ""              # "" / "check" / "download"
        self._upd_manual = False         # 这次是用户手动点的? (只有手动才提示「已是最新」)
        self._upd_progress = None        # (已下载, 总字节) —— 菜单项标题显示进度
        self._upd_pending = None         # 查到的新版本, 等用户点「下载并安装」
        self._upd_started = time.time()
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
                        "rclick": 2, "down": 0, "binds": HB.binds_from_cfg(self.cfg),
                        "flip": False, "error": ""}

            _dev = (("小米平板 9 Pro Max", 0x18D1, 0x2D05, True),)
            for _tag, _st_ in (("权限不齐", _fake_st(False, False, -536870174)),
                               ("只缺输入监控", _fake_st(True, False, -536870174)),
                               ("权限就绪", _fake_st(True, True, 0, _dev))):
                _p("=== 设置窗口: %s ===" % _tag)
                self.win.refresh(_st_, force=True)
                UI.dump_layout(self.win.win.contentView())
                _probs = UI.check_layout(self.win.win.contentView())
                _probs += UI.check_layout(self.win.adv.win.contentView(), UI.AW, UI.AH)
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
            _p("=== 按键绑定表 (唯一出处 hid_bridge.BIND_GESTURES) ===")
            for _g, _t, _opts, _d in HB.BIND_GESTURES:
                _p("  %-6s 默认=%-8s 可绑: %s"
                   % (_t, HB.action_title(_g, _d),
                      " / ".join(HB.action_title(_g, a) for a in _opts)))
            _p("  当前绑定: %s" % HB.binds_from_cfg(self.cfg))
            _p("=== 老配置迁移 (升级后手感不变) ===")
            for _leg in ({"mode": "scroll", "hold_drag": True, "rc_hold_ms": 800},
                         {"mode": "select"},
                         {"mode": "scroll", "hold_drag": False, "rc_hold_ms": 0}):
                _p("  %-58s -> %s" % (str(_leg), HB.binds_from_cfg(_leg)))
            _p("=== 高级选项窗口 (独立小窗) ===")
            self.win.adv.refresh(force=True)
            UI.dump_layout(self.win.adv.win.contentView())
            _ap = UI.check_layout(self.win.adv.win.contentView(), UI.AW, UI.AH)
            _p("布局检查: %s" % ("全部通过" if not _ap else "%d 处问题" % len(_ap)))
            for _pr in _ap:
                _p("  !! %s" % _pr)
            _p("=== 按键绑定: 下拉 -> app -> engine -> Bridge 全链路 ===")
            _save_binds = globals()["save_cfg"]
            globals()["save_cfg"] = lambda cfg: None      # 别真写进用户配置
            try:
                _before = dict(HB.binds_from_cfg(self.cfg))
                self.set_bind("tap", "double")
                self.set_bind("swipe", "drag")
                self.win.refresh(force=True)
                _p("  改后 cfg      : %s" % HB.binds_from_cfg(self.cfg))
                _p("  下拉选中的项  : 轻点=%r 快速滑动=%r"
                   % (self.win.c["bind_tap"].titleOfSelectedItem(),
                      self.win.c["bind_swipe"].titleOfSelectedItem()))
                _p("  Bridge 实际生效: %s" % (self.engine.b.bind,))
                _p("  滚动方向可用  : %s (没有手势绑滚动 -> 应为 False)"
                   % bool(self.win.c["nat"].isEnabled()))
                _p("  行尾说明      : 快速滑动=%r 停住再滑=%r 停住不动=%r"
                   % (self.win.c["bind_swipe_hint"].stringValue(),
                      self.win.c["bind_hold_swipe_hint"].stringValue(),
                      self.win.c["bind_hold_hint"].stringValue()))
                self.reset_binds()
                self.win.refresh(force=True)
                _ok = (HB.binds_from_cfg(self.cfg) == dict(HB.DEFAULT_BINDS)
                       and self.engine.b.bind == dict(HB.DEFAULT_BINDS)
                       and self.win.c["bind_tap"].titleOfSelectedItem()
                       == HB.action_title("tap", HB.DEFAULT_BINDS["tap"]))
                _p("  恢复默认后    : cfg=%s 下拉=%r -> %s"
                   % (HB.binds_from_cfg(self.cfg),
                      self.win.c["bind_tap"].titleOfSelectedItem(),
                      "通过" if _ok else "不通过"))
                # 新动作「切换屏幕」必须在每个手势的下拉里真选得到, 选了还要一路传到
                # Bridge (下拉 -> app -> engine -> Bridge), 光在表里加了不算数
                _miss = []
                for _g2, _t2, _o2, _d2 in HB.BIND_GESTURES:
                    _ctl = self.win.c.get("bind_" + _g2)
                    _items = [str(_ctl.itemTitleAtIndex_(_i))
                              for _i in range(_ctl.numberOfItems())]
                    _p("  下拉 %s: %s" % (_t2, " / ".join(_items)))
                    if "切换屏幕" not in _items:
                        _miss.append(_t2)
                self.set_bind("hold", "screen")
                self.win.refresh(force=True)          # set_bind 只管状态, 界面靠这一下同步
                _p("  停住不动=切换屏幕 -> 下拉=%r Bridge=%r"
                   % (self.win.c["bind_hold"].titleOfSelectedItem(),
                      self.engine.b.bind.get("hold")))
                _sok = (not _miss
                        and self.win.c["bind_hold"].titleOfSelectedItem()
                        == HB.action_title("hold", "screen")
                        and self.engine.b.bind.get("hold") == "screen")
                _p("  切换屏幕可选  : %s%s" % ("通过" if _sok else "不通过",
                                             "" if not _miss else " 缺: %s" % _miss))
                for _g, _a in _before.items():
                    self.set_bind(_g, _a)                 # 还原成用户原来那份
                self.win.refresh(force=True)
            finally:
                globals()["save_cfg"] = _save_binds
            # 同一个坑在窗口里也会静默失效 (setAction_/setTarget_ 配错 -> 点了没反应、还不报错),
            # 所以每个控件的动作都当场问一次 ObjC: "这个选择器你真的认识吗?"
            _p("=== 控件动作审计 (每个动作都必须是真选择器) ===")
            _wbad, _wn = [], 0
            for _won, _wo in (("设置窗口", self.win), ("高级选项", self.win.adv)):
                for _k, _ctl in sorted(getattr(_wo, "c", {}).items()):
                    try:
                        _a = _ctl.action()
                    except Exception:
                        continue
                    if not _a:
                        continue
                    _a = str(_a)
                    _t = _ctl.target()
                    _wn += 1
                    if _t is not None:
                        _ok = bool(_t.respondsToSelector_(_a.encode()))
                    else:
                        _ok = any(_x.respondsToSelector_(_a.encode())
                                  for _x in (self, self.win, self.win.adv))
                    if not _ok:
                        _wbad.append((_won, _k, _a))
                        _p("  !! %s / %s (%s) action=%r 派发不到"
                           % (_won, _k, type(_ctl).__name__, _a))
                    elif _a.endswith("_:"):
                        _p("  ~ %s / %s (%s) action=%r 带下划线, 可疑"
                           % (_won, _k, type(_ctl).__name__, _a))
            _p("  有动作的控件 %d 个, 派发不到的 %d 个" % (_wn, len(_wbad)))
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
            _all = list(self.win.c.items()) + [("高级·%s" % _k, _v)
                                               for _k, _v in self.win.adv.c.items()]
            for _k, _v in _all:
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
            if _st in ("wiring", "exit"):
                # 菜单项的**点击路径**必须单独验: 直接调 self.actTarget_(it) 只证明 Python 方法
                # 能跑, 证明不了 AppKit 会不会把点击派发到它 (target/action/selector 任一处不对,
                # 菜单看着正常、点了却静默无反应)。这里用菜单自己的派发入口。
                print("=== 菜单项的点击路径 (ObjC 层) ===")
                _sm = self.mg_tgt
                _p = self.mi_tgt_parent
                _btn = self.item.button()
                _bf = _btn.window().frame()
                print("状态栏按钮 frame = (%.0f,%.0f) %.0fx%.0f"
                      % (_bf.origin.x, _bf.origin.y, _bf.size.width, _bf.size.height))
                _ms = self.item.menu().size()
                _ss = _sm.size()
                print("主菜单 size = %.0fx%.0f | 光标基准屏子菜单 size = %.0fx%.0f | 子菜单项数 %d"
                      % (_ms.width, _ms.height, _ss.width, _ss.height, _sm.numberOfItems()))
                print("父项: title=%r action=%r target=%r enabled=%s"
                      % (_p.title(), _p.action(), _p.target(), bool(_p.isEnabled())))
                _orig = str(self.cfg.get("target_display") or DP.TARGET_AUTO)
                for _i, _it in enumerate(_sm.itemArray()):
                    if _it.isSeparatorItem():
                        print("  [%d] ----" % _i)
                        continue
                    print("  [%d] %-22r action=%r target=%r tag=%s enabled=%s state=%s"
                          % (_i, _it.title(), _it.action(), _it.target(), _it.tag(),
                             bool(_it.isEnabled()), _it.state()))
                    _tag = int(_it.tag())
                    _want = self._tgt_opts[_tag][0] if 0 <= _tag < len(self._tgt_opts) else None
                    if _want is None or _want == _orig or not _it.action():
                        print("      跳过 (want=%s, 当前 %s)" % (_want, _orig))
                        continue
                    print("      performActionForItemAtIndex_(%d) -> 期望切到 %s" % (_i, _want))
                    try:
                        _sm.performActionForItemAtIndex_(_i)
                        print("      现在: 配置=%s 生效=%s 打勾=%s"
                              % (self.cfg.get("target_display"),
                                 (self.engine.target or {}).get("value"),
                                 [x.title() for x in self.mi_tgt if x.action() and x.state()]))
                    except Exception as _e:
                        print("      !! 派发失败: %r" % (_e,))
                self.set_target(_orig)
                print("已还原 -> 配置 %s" % (self.cfg.get("target_display"),))
                print("=== 全菜单动作审计 (选择器必须真实存在) ===")
                _bad, _n = [], 0
                for _mi in (self.item.menu(), self.mg_tgt, self.mg_disp):
                    for _it in _mi.itemArray():
                        if _it.isSeparatorItem() or not _it.action():
                            continue
                        # 子菜单父项的 submenuAction: 是 AppKit 自己挂的, 不归我们管
                        if str(_it.action()) == "submenuAction:":
                            continue
                        _a = str(_it.action())
                        _t = _it.target()
                        _n += 1
                        _ok = bool(_t is not None and _t.respondsToSelector_(_a.encode()))
                        _warn = " <- 带下划线, 像是把 Python 方法名当选择器了" \
                            if _a.endswith("_:") else ""
                        print("  [%s] %-22r action=%-24r 派发得到=%s%s"
                              % (_mi.title(), _it.title(), _a, _ok, _warn))
                        if not _ok:
                            _bad.append((_mi.title(), _it.title(), _a))
                print("有动作的项 %d 个, 派发不到的 %d 个" % (_n, len(_bad)))
                for _b in _bad:
                    print("  !! 死项: 菜单 %r / 项 %r / action=%r" % _b)
                print("结论: %s"
                      % ("点击路径通 (每个动作都能派发)" if not _bad
                         else "**有 %d 个点不动的项**" % len(_bad)))
            sys.stdout.flush()
            if _st in ("wiring", "exit"):
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
        _ssc = os.environ.get("DPTOUCH_SELFTEST_SCREEN")
        if _ssc:
            # 无头自检: 绑定动作「切换屏幕」整条链路 —— 笔上按一下 = HID 线程里的
            # engine.next_screen(); 主线程那一半 (取待办、写配置、把菜单的勾挪过去)
            # 就在 _tick_body 里, 所以这里直接调真的 _tick_body, 不另写一份模拟。
            # 断言的重点是「勾」: 上一版切完勾不动, 用户第一眼就看这个。
            _bad = []
            print("=== 切换屏幕: 按顺序遍历的顺序 ===")
            for _d in DP.order_displays():
                print("  id=%-4s %-26s %s%s"
                      % (_d["id"], _d.get("name"), DP.bounds_of(_d),
                         " (自带屏)" if _d.get("builtin") else ""))
            _orig = str(self.cfg.get("target_display") or DP.TARGET_AUTO)
            _seq0 = DP.order_displays()
            _vals = [v for v, _t in DP.target_choices()]
            print("=== 从最左那块 (%s) 起, 每按一次切下一块; 原配置 %s ==="
                  % (_seq0[0].get("name"), _orig))
            self.set_target(str(_seq0[0]["id"]))
            self._tick_body()
            for _i in range(min(len(_seq0), 4)):
                _v = self.engine.next_screen()          # ← 笔上按的那一下
                self._tick_body()                       # ← 主线程那一拍
                _r = self.engine.target or {}
                _b = self.engine.b
                _mark = [it.title() for it in self.mi_tgt
                         if it.action() is not None and it.state()]
                _title = self.mi_tgt_parent.title()
                _want = dict(self._tgt_opts).get(_v, "")
                _ok = bool(_v in _vals and _mark == [_want] and _want
                           and _title.startswith("光标基准屏："))
                if not _ok:
                    _bad.append(_v)
                print("  按 %d -> 值 %-7s 生效 %-10s 接管=%-5s 矩形=(%.0f,%.0f) %.0fx%.0f "
                      "菜单=%s 打勾=%s 配置=%s %s"
                      % (_i + 1, _v, _r.get("value"), _r.get("takeover"), _b.ox, _b.oy,
                         _b.w, _b.h, _title, _mark, self.cfg.get("target_display"),
                         "" if _ok else "!! 勾/标题没跟上"))
                sys.stdout.flush()
            self.set_target(_orig)
            print("已还原 -> %s (配置 %s)"
                  % (self.engine.target.get("label"), self.cfg.get("target_display")))
            if _ssc == "hidsim":
                # 用户点名的用法: 停住不动 -> 切换屏幕。这里喂合成 HID 报文走真实链路
                # (feed -> 绑定判定 -> 动作 -> 换基准屏), 顺带证明它不发任何鼠标键。
                print("=== 笔上真按一次: 停住不动 -> 切换屏幕 ===")
                _ob = str(self.cfg.get("bind_hold") or "right")
                _t0 = str(self.cfg.get("target_display") or DP.TARGET_AUTO)
                self._apply(bind_hold="screen")
                _e = self.engine
                _e.feed(0x01, 0x30, 50, 0, 100)      # 笔尖悬停在正中
                _e.feed(0x01, 0x31, 50, 0, 100)
                _e.feed(0x0D, 0x42, 1, 0, 1)         # 落下
                _e.b.hold_t0 -= 1.0                  # 停住一秒
                _e.feed(0x0D, 0x42, 0, 0, 1)         # 抬手 -> 「停住不动」就在这一刻判定
                _v = _e.pending_target          # 只看一眼, 别取走 (取走 tick 就补不上界面了)
                _b = _e.b
                self._tick_body()
                _mark = [it.title() for it in self.mi_tgt
                         if it.action() is not None and it.state()]
                _want = dict(self._tgt_opts).get(_v, "")
                _ok = bool(_v and _v != _t0 and not (_b.nrc or _b.nd or _b.nclk)
                           and _mark == [_want] and _want)
                if not _ok:
                    _bad.append("hidsim")
                print("  笔上长按 -> 待办 %-7s 原基准屏 %-7s 右键 %d / 拖拽 %d / 轻点 %d"
                      % (_v, _t0, _b.nrc, _b.nd, _b.nclk))
                print("  菜单=%s 打勾=%s %s"
                      % (self.mi_tgt_parent.title(), _mark,
                         "" if _ok else "!! 没切过去, 或者发了鼠标键"))
                self._apply(bind_hold=_ob)
                self.set_target(_orig)
                print("已还原: 停住不动=%s 基准屏=%s"
                      % (self.cfg.get("bind_hold"), self.cfg.get("target_display")))
            print("结论: %s" % ("每一步的勾和标题都跟上了" if not _bad
                              else "!! %d 步没跟上: %r" % (len(_bad), _bad)))
            sys.stdout.flush()
            NSApplication.sharedApplication().terminate_(None)
            return
        _su = os.environ.get("DPTOUCH_SELFTEST_UPDATE")
        if _su:
            # 无头自检: 更新这条链路。三档 ——
            #   offline 纯离线: 版本号比较 / 正文里抓 SHA256 / 更新脚本内容 (不联网)
            #   live    真去 GitHub 查一次。重点是**在打包后的 app 里**跑: PyInstaller
            #           少带 _ssl.so 这类坑只在 bundle 里才现形, 源码里跑是绿的也没用
            #   dry     假装本地是 0.0.1 -> 真下载最新版 + 真校验 SHA256, 但不换包
            _bad = []

            def _p(s):                # 不 flush 的 stdout 在 terminate 时会整段丢
                print(s, flush=True)

            _local = "0.0.1" if _su == "dry" else APP_VERSION
            _p("=== 检查更新: 本地 %s | 模式 %s ===" % (_local, _su))
            _ok0, _l0 = UPD.selftest_offline()
            for _l in _l0:
                _p("  " + _l)
            if not _ok0:
                _bad.append("offline")
            if _su != "offline":
                _t0 = time.time()
                try:
                    _r = UPD.check(_local)
                except Exception as _e:
                    _r = {"ok": False, "why": "%r" % (_e,)}
                _p("  查询 GitHub: ok=%s 最新=%s 有新版本=%s 用时 %.1fs"
                   % (_r.get("ok"), _r.get("version"), _r.get("newer"), time.time() - _t0))
                if not _r.get("ok"):
                    _p("  原因: %s" % _r.get("why"))
                    _bad.append("check")
                else:
                    if not isinstance(_r.get("newer"), bool):
                        _bad.append("newer 不是布尔")
                    if _r.get("newer"):
                        _p("  资产: %s (%s)" % (_r.get("asset") or "?",
                                               UPD.human_size(_r.get("size") or 0)))
                        _p("  更新说明: %s" % (_r.get("notes") or "").replace("\n", " ")[:120])
                        if not _r.get("sha256"):
                            _bad.append("新版本的发版正文里没抓到 SHA256")
                        else:
                            _p("  正文里的 SHA256: %s…" % _r["sha256"][:16])
                        if not (_r.get("url") or "").startswith("https://"):
                            _bad.append("下载链接不是 https")
                    else:
                        _p("  (已经是最新, 按设计不带下载链接和校验值)")
                    if _su == "dry" and _r.get("newer"):
                        _t1 = time.time()
                        _dst = UPD.new_dmg_path(_r.get("version"))
                        try:
                            _fp, _sha = UPD.download(_r["url"], _dst,
                                                     progress=lambda g, t: None,
                                                     expected_sha=_r.get("sha256"))
                            _sz = os.path.getsize(_fp)
                            _same = bool(_r.get("sha256")) and _sha == _r["sha256"]
                            _p("  下载 + 校验: %s (%s, %.1fs) -> %s"
                               % (_fp, UPD.human_size(_sz), time.time() - _t1,
                                  "SHA256 和发版正文一致" if _same else "正文没写校验值"))
                            if _r.get("sha256") and not _same:
                                _bad.append("下载下来的 SHA256 和正文对不上")
                            if _r.get("size") and _sz != _r["size"]:
                                _bad.append("落盘大小和 API 报的资产大小不一致")
                            _sc = UPD.write_installer("/Applications/x.app", _fp, 1)
                            _p("  换包脚本预演: %s (%d 字节, 已落地且可执行=%s)"
                               % (_sc, os.path.getsize(_sc), os.access(_sc, os.X_OK)))
                            os.unlink(_sc)
                            os.unlink(_fp)      # 自检不留安装包
                        except Exception as _e:
                            _p("  下载或校验失败: %r" % (_e,))
                            _bad.append("dry")
            if _su == "flow":
                # 走到「查到新版本」之后的那些分支: 弹窗被 DPTOUCH_NO_ALERT 拦下 (只记日志),
                # 所以这里能安全地验「取后台结果 -> 分派 -> 不误写配置」这一段。
                _keep_last = self.cfg.get("update_last_check")
                _keep_skip = self.cfg.get("update_skip")
                self._upd_manual = True
                self._upd_busy = "check"
                self._upd_in = ("check", {"ok": True, "newer": True, "version": "9.9.9",
                                          "asset": "fake.dmg", "size": 1234,
                                          "url": "https://example.invalid/x.dmg",
                                          "sha256": "c" * 64, "notes": "自检用的假版本"})
                self._upd_tick()
                _p("  「查到新版本」-> 忙闲=%r 上次检查已记下=%s 跳过版本=%r (应为空)"
                   % (self._upd_busy, bool(self.cfg.get("update_last_check")),
                      self.cfg.get("update_skip")))
                if self._upd_busy or not self.cfg.get("update_last_check"):
                    _bad.append("后台结果没有被主线程取走")
                if self.cfg.get("update_skip"):
                    _bad.append("无头模式下不该写 update_skip")
                self._upd_manual = True
                self._upd_in = ("check", {"ok": False, "newer": False,
                                          "why": "自检: 假装连不上 GitHub"})
                self._upd_busy = "check"
                self._upd_tick()
                _p("  「检查失败」-> 忙闲=%r (应为空, 失败不能把状态卡住)" % self._upd_busy)
                if self._upd_busy:
                    _bad.append("失败后忙闲没清空")
                self._upd_manual = True
                self._upd_busy = "download"
                self._upd_progress = (7, 10)
                self._refresh_check_item()
                _p("  下载中菜单项标题: %r" % self.mi_check.title())
                if "70%" not in self.mi_check.title():
                    _bad.append("下载进度没反映到菜单项标题")
                self._upd_busy = ""
                self._upd_progress = None
                self._refresh_check_item()
                if "检查更新" not in self.mi_check.title():
                    _bad.append("菜单项标题没复原成「检查更新…」")
                self._apply(update_last_check=_keep_last, update_skip=_keep_skip)
                _p("  已还原: 上次检查=%r 跳过版本=%r" % (self.cfg.get("update_last_check"),
                                                          self.cfg.get("update_skip")))
            _p("结论: %s" % ("全部通过" if not _bad else "!! %r" % _bad))
            sys.stdout.flush()
            NSApplication.sharedApplication().terminate_(None)
            return
        _stg = os.environ.get("DPTOUCH_SELFTEST_TARGET")
        if _stg:
            # 无头自检: 真的走一遍「光标基准屏」切换 (和菜单动作同一条路径), 跑完还原配置
            print("=== 光标基准屏: 可选项 ===")
            for _v, _t in self.target_choices():
                print("  %-8s %s" % (_v, _t))
            _orig = str(self.cfg.get("target_display") or DP.TARGET_AUTO)
            print("=== 切换 (原值 %s) ===" % _orig)
            for _v in [s.strip() for s in _stg.split(",") if s.strip()]:
                self.set_target(_v)
                _r = self.engine.target or {}
                _b = self.engine.b
                _mark = [it.title() for it in self.mi_tgt
                         if it.action() is not None and it.state()]
                print("切到 %-7s -> 生效 %-10s 接管=%-5s 矩形=(%.0f,%.0f) %.0fx%.0f "
                      "菜单=%s 打勾=%s 配置=%s"
                      % (_v, _r.get("value"), _r.get("takeover"), _b.ox, _b.oy, _b.w, _b.h,
                         self.mi_tgt_parent.title(), _mark, self.cfg.get("target_display")))
            self.set_target(_orig)
            print("已还原 -> %s (配置 %s)"
                  % (self.engine.target.get("label"), self.cfg.get("target_display")))
            sys.stdout.flush()
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
        if action:
            action = _sel(action)
            if not self.respondsToSelector_(action.encode()):
                # 名字写错时 AppKit 会静默丢掉点击 —— 宁可日志里吵, 也不要用户点了没反应
                log("!! 菜单项动作不是合法选择器: %r <- %r (点击会静默失效)"
                    % (title, action))
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

        # --- 光标基准屏: 笔的绝对坐标铺到哪块屏上 (接了第二块屏时, 靠它换屏) ---
        self.mi_tgt_parent = self._sub(m, "光标基准屏")
        self.mg_tgt = self.mi_tgt_parent.submenu()
        self.mi_tgt = []         # 动态项, 每次整组重建
        self._tgt_opts = []      # 当前列出的条目 (tag -> 下标)
        self._tgt_tick = 0
        self._sep(m)

        self._mk(m, "设置…", "actOpenWindow:", key=",")
        self._mk(m, "打开日志", "actOpenLog:")
        # 更新只在菜单里占一行: 标题会随进度变成「正在下载更新 42%」
        self.mi_check = self._mk(m, "检查更新…", "actCheckUpdate:")
        self._sep(m)
        self._mk(m, "退出 %s" % APP_NAME, "actQuit:", key="q")

        self.item.setMenu_(m)
        m.setDelegate_(self)      # 菜单显示期间不许改它 (见 menuWillOpen_ / tick_)
        self._reflect()
        self._sync_display(force=True)
        self._sync_target_menu(force=True)

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
    def _sync_target_menu(self, force=False):
        """重建「光标基准屏」子菜单。

        条目 = 跟随光标 + 平板那块屏 + 每块在线屏。父项标题直接写上当前值 ——
        菜单栏 App 一眼能看出笔现在铺在哪块屏上, 不用点开。
        """
        if getattr(self, "_menu_open", False) and not force:
            return            # 菜单正显示着: 不许改它 (见 tick_ 的说明)
        self._tgt_tick += 1
        if not force and (self._tgt_tick - 1) % 10 != 0:
            return
        for it in self.mi_tgt:
            if it.menu() is self.mg_tgt:
                self.mg_tgt.removeItem_(it)
        self.mi_tgt = []
        self._tgt_opts = self.target_choices()
        cur = str(self.cfg.get("target_display") or DP.TARGET_AUTO)
        for i, (val, title) in enumerate(self._tgt_opts):
            if val != DP.TARGET_AUTO and i > 0 and self._tgt_opts[i - 1][0] == DP.TARGET_AUTO:
                self.mi_tgt.append(NSMenuItem.separatorItem())
                self.mg_tgt.addItem_(self.mi_tgt[-1])
            self.mi_tgt.append(self._mk(self.mg_tgt, title, "actTarget:",
                                        tag=i, check=(val == cur)))
        short = next((t for v, t in self._tgt_opts if v == cur), "跟随光标").split(" · ")[0]
        if len(short) > 12:
            short = short[:11] + "…"
        self.mi_tgt_parent.setTitle_("光标基准屏：%s" % short)

    @objc.python_method
    def _tick_body(self):
        # 笔上「切换屏幕」: 生效那一半在 HID 线程里已经做完了 (笔要立刻能用), 这里补上
        # 主线程这一半 —— 写配置 + 让菜单的勾跟上。菜单一秒内刷新, 勾就跟着走。
        _v = self.engine.take_pending_target()
        if _v:
            self.set_target(_v)
        st = self.engine.status()
        self.mi_status.setTitle_(self.engine.status_line_short())
        self._reflect()
        self.win.refresh(st)         # 窗口没开时它自己会立刻返回
        self._sync_display()
        self._sync_target_menu()
        self._upd_tick()             # 检查更新: 排期 + 取后台结果

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
        it = self._mk(self.mg_disp, "退回切换前的档位", "actDisplayRestore:")
        if not self.cfg.get("display_saved"):
            it.setEnabled_(False)
        self.mi_disp.append(it)
        self.mi_disp.append(NSMenuItem.separatorItem())
        self.mi_disp.append(self._mk(self.mg_disp, "重新扫描显示器",
                                     "actDisplayRescan:"))
        self.mi_disp.append(self._mk(self.mg_disp, "打开「显示器」设置…",
                                     "actOpenDisplaySettings:"))

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
        self._sync_target_menu(force=True)      # 「平板那块屏」这一项跟着它出现/消失

    # ---------------- 光标基准屏 ----------------

    @objc.python_method
    def target_choices(self):
        """「光标基准屏」的条目 (值, 标题) —— 菜单与高级选项共用这一份。

        值 = "auto"(跟随光标) / "tablet"(平板那块屏) / "<displayID>"(具体某块屏)。
        """
        return DP.target_choices(allow_unknown=bool(self.cfg["display_allow_unknown"]))

    @objc.python_method
    def set_target(self, value):
        """把笔的绝对坐标铺到哪块屏上 —— 决定笔能不能到别的屏去。

        "auto"   不接管坐标: 光标在哪块屏, 笔就在那块屏生效 (老行为, 默认)
        其它值   接管坐标: 笔移到哪, 光标就在那块屏上到哪 —— 多屏时靠它换屏
        """
        value = str(value or DP.TARGET_AUTO)
        if value == str(self.cfg.get("target_display") or DP.TARGET_AUTO):
            return
        self._apply(target_display=value)
        r = getattr(self.engine, "target", None) or {}
        log("光标基准屏 -> %s (%s)" % (r.get("label", value), value))
        if r.get("note"):
            log("光标基准屏提示: %s" % r["note"])
        self._sync_target_menu(force=True)
        try:
            self.win.refresh(force=True)
            self.win.adv.refresh(force=True)
        except Exception:
            pass

    def actTarget_(self, sender):
        """菜单里选了「光标基准屏」—— tag 是 _tgt_opts 里的下标 (和显示缩放同一套)"""
        opts = list(self._tgt_opts)
        i = max(0, int(sender.tag()))
        if i < len(opts):
            self.set_target(opts[i][0])

    def actDragPen_(self, sender):
        """拖拽位置源改用笔的绝对坐标 (高级选项; 系统光标拖不动时才需要)"""
        self._apply(drag_pen=not bool(self.cfg["drag_pen"]))

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
    def set_bind(self, gesture, action):
        """改一个手势绑的动作 (窗口下拉与「恢复默认」都走这里)。"""
        if gesture not in HB.DEFAULT_BINDS or action not in HB.BIND_TITLES:
            return
        kw = {HB.bind_key(gesture): action}
        # 「停住不动」的判定时长本来就是 0 (=关) 的话, 绑上动作顺手把它打开 ——
        # 否则用户绑了却永远触发不了, 看着就是"没反应"。
        if gesture == "hold" and action != "none" and not int(self.cfg.get("rc_hold_ms") or 0):
            kw["rc_hold_ms"] = 800
        self._apply(**kw)
        log("按键绑定: %s -> %s" % (HB.gesture_title(gesture), HB.action_title(gesture, action)))

    @objc.python_method
    def reset_binds(self):
        """「恢复默认」: 写回 DEFAULT_BINDS (默认 = 本工具一直以来的行为)。"""
        self._apply(**{HB.bind_key(g): a for g, a in HB.DEFAULT_BINDS.items()})
        log("按键绑定: 已恢复默认")

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
        """「停住再滑」判定多久算停住 (ms)"""
        import hid_bridge as HB2
        try:
            ms = int(ms)
        except Exception:
            return
        if ms not in HB2.HOLD_MS_OPTS:
            return
        self._apply(hold_ms=ms)
        log("「停住再滑」判定 -> %d ms" % ms)

    @objc.python_method
    def set_rc_hold(self, ms):
        """「停住不动」判定多久 (0 = 关)"""
        import hid_bridge as HB2
        try:
            ms = int(ms)
        except Exception:
            return
        if ms not in HB2.RC_HOLD_MS_OPTS:
            return
        self._apply(rc_hold_ms=ms)
        log("「停住不动」判定 -> %s" % ("关" if not ms else "%.1f s" % (ms / 1000.0)))

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

    # ---------------- 检查更新 ----------------
    #
    # 职责切分: 网络与文件逻辑全在 dptouch_update 里 (纯函数, 离线可测, 见
    # tools/selftest_update.py); 这里只管「什么时候查」「弹什么」「退出之后谁去换包」。
    # 后台线程只往 self._upd_in 里放结果, 主线程每秒那一拍取走 ——
    # 从别的线程里碰 AppKit 是雷 (这台机器上踩过)。

    UPD_ITEM_TITLE = "检查更新…"
    UPD_AUTO_DELAY = 12          # 启动后先等一会儿再静默查, 不和启动抢
    UPD_INTERVAL = 24 * 3600     # 之后每天一次

    @objc.python_method
    def installed_app_path(self):
        """本程序自己的 .app 路径; 开发态 (直接跑 src/dptouch.py) 返回空串。

        更新只对装好的 App 有意义 —— 开发态没有可替换的 bundle。
        """
        try:
            b = NSBundle.mainBundle().bundlePath()
        except Exception:
            b = ""
        return b if b.endswith(".app") else ""

    @objc.python_method
    def update_due(self, now=None, last=None):
        """该不该自动查一次。判据只有这一处, 自检直接调它。"""
        if not self.cfg.get("update_auto", True):
            return False
        if not self.installed_app_path():
            return False
        if any(k.startswith("DPTOUCH_SELFTEST_") for k in os.environ):
            return False                     # 自检期间不联网, 别把自检结果搅浑
        if self._upd_busy:
            return False
        now = time.time() if now is None else now
        last = float(self.cfg.get("update_last_check") or 0) if last is None else float(last)
        if last <= 0:
            return now - self._upd_started >= self.UPD_AUTO_DELAY
        return now - last >= self.UPD_INTERVAL

    @objc.python_method
    def check_update(self, manual=False):
        if self._upd_busy:
            if manual:
                self._warn("正在忙", "上一次检查或下载还没结束, 稍等一下。")
            return False
        if not self.installed_app_path():
            if manual:
                self._warn("开发态不检查更新", "更新是给装好的 App 用的。")
            return False
        self._upd_busy = "check"
        self._upd_manual = bool(manual)
        log("检查更新: %s" % ("手动" if manual else "自动"))
        threading.Thread(target=self._upd_worker_check, daemon=True).start()
        return True

    @objc.python_method
    def _upd_worker_check(self):
        try:
            res = UPD.check(APP_VERSION)
        except Exception as e:               # 线程里抛出去的异常没人接, 兜住
            res = {"ok": False, "newer": False,
                   "why": "检查出错（%s: %s）" % (e.__class__.__name__, e)}
        self._upd_in = ("check", res)

    @objc.python_method
    def _upd_worker_download(self, res):
        try:
            dmg = UPD.new_dmg_path(res.get("version"))
            UPD.download(res["url"], dmg, progress=self._upd_set_progress,
                         expected_sha=res.get("sha256"))
            out = {"ok": True, "path": dmg}
        except Exception as e:
            out = {"ok": False, "why": str(e), "path": ""}
        self._upd_in = ("dl", out)

    @objc.python_method
    def _upd_set_progress(self, got, total):
        self._upd_progress = (got, total)     # 主线程那一拍读它改菜单项标题

    @objc.python_method
    def _refresh_check_item(self):
        it = getattr(self, "mi_check", None)
        if it is None:
            return
        want = self.UPD_ITEM_TITLE
        if self._upd_busy == "check":
            want = "正在检查更新…"
        elif self._upd_busy == "download":
            pg = self._upd_progress
            want = ("正在下载更新 %d%%" % (100 * pg[0] // pg[1])
                    if pg and pg[1] else "正在下载更新…")
        if it.title() != want:
            it.setTitle_(want)

    @objc.python_method
    def _upd_tick(self):
        """每秒那一拍: 排期 + 取后台结果 + 刷新菜单项标题。"""
        if not self._upd_busy and self.update_due():
            self.check_update(manual=False)
        p = self._upd_in
        if p:
            self._upd_in = None
            kind, data = p
            self._upd_busy = ""
            self._upd_progress = None
            if kind == "check":
                self.cfg["update_last_check"] = int(time.time())
                save_cfg(self.cfg)
                self._upd_on_check(data)
            else:
                self._upd_on_download(data)
        self._refresh_check_item()

    @objc.python_method
    def _ask(self, title, body, buttons):
        """要用户选一个的问询。返回被点按钮的下标; 无头模式返回 None。

        和 _warn 分开: 那个是「知道了」, 这个是「选一个」。
        """
        if os.environ.get("DPTOUCH_NO_ALERT"):
            log("!! %s: %s [%s] (无头模式: 不弹窗)" % (title, body, " / ".join(buttons)))
            return None
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(body)
        for b in buttons:
            a.addButtonWithTitle_(b)
        return int(a.runModal()) - 1000        # NSAlertFirstButtonReturn == 1000

    @objc.python_method
    def _upd_on_check(self, res):
        manual = self._upd_manual
        self._upd_manual = False
        if not res.get("ok"):
            log("检查更新失败: %s" % res.get("why"))
            if manual:
                self._warn("检查更新失败", res.get("why") or "原因不明。")
            return
        v = res.get("version") or ""
        if not res.get("newer"):
            log("检查更新: 已是最新 (%s)" % v)
            if manual:
                self._warn("已是最新", "当前版本 %s 就是最新版。" % APP_VERSION)
            return
        if not manual and v and v == str(self.cfg.get("update_skip") or ""):
            log("检查更新: %s 上次选了「以后再说」, 不再自动提醒" % v)
            return
        log("检查更新: 有新版本 %s (%s %s)"
            % (v, res.get("asset") or "", UPD.human_size(res.get("size") or 0)))
        self._upd_pending = res
        body = "当前 %s → 最新 %s%s\n\n%s" % (
            APP_VERSION, v,
            "（下载 %s）" % UPD.human_size(res.get("size") or 0) if res.get("size") else "",
            res.get("notes") or "（这一版没有写更新说明）")
        pick = self._ask("有新版本 %s" % v, body, ["下载并安装", "以后再说"])
        if pick is None:
            return
        if pick == 0:
            self._upd_download(res)
        else:
            self._apply(update_skip=v)      # 这一版不再自动提醒; 手动查还查得到
            log("更新: 用户选了「以后再说」(%s)" % v)

    @objc.python_method
    def _upd_download(self, res):
        if self._upd_busy:
            return
        self._upd_busy = "download"
        self._upd_progress = (0, res.get("size") or 0)
        self._refresh_check_item()
        log("更新: 开始下载 %s" % res.get("url"))
        threading.Thread(target=self._upd_worker_download, args=(res,), daemon=True).start()

    @objc.python_method
    def _upd_on_download(self, out):
        if not out.get("ok"):
            log("更新: 下载或校验失败 —— %s" % out.get("why"))
            self._warn("下载失败", "%s\n\n可以过一会儿再试, 或去 GitHub 页面手动下载。"
                       % out.get("why"))
            return
        log("更新: 下载并校验通过 -> %s" % out["path"])
        pick = self._ask("更新已就绪",
                         "点「立即更新」会退出程序, 自动替换并重新打开（几秒）。\n"
                         "更新后如果笔又不好使了, 在设置窗口里重新授权一次即可。",
                         ["立即更新", "稍后"])
        if pick is None:
            return
        if pick != 0:
            log("更新: 用户选了「稍后」, 安装包留在 %s" % out["path"])
            return
        self.install_update(out["path"])

    @objc.python_method
    def install_update(self, dmg):
        """写出安装脚本 -> 起一个脱离本进程的 sh 去换包 -> 硬退。

        换包不能在本进程里做: 要替换的正是正在跑的这份 bundle。脚本里每一处路径都带
        引号, 且任何一步失败都回滚 (见 dptouch_update.installer_script)。
        """
        app = self.installed_app_path()
        if not app:
            self._warn("不能自动更新", "当前不是安装版。")
            return
        try:
            script = UPD.write_installer(app, dmg, os.getpid())
        except Exception as e:
            log("更新: 写安装脚本失败 —— %r" % (e,))
            self._warn("更新失败", "写安装脚本失败：%s" % e)
            return
        log("更新: 交给后台脚本换包 (本进程 pid %d) -> %s" % (os.getpid(), script))
        try:
            self.engine.stop()
        except Exception:
            pass
        subprocess.Popen(["/bin/sh", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)      # 硬退: 脚本在等这个 pid 消失

    def actCheckUpdate_(self, sender=None):
        self.check_update(manual=True)

    def actUpdateAuto_(self, sender=None):
        self._apply(update_auto=not bool(self.cfg.get("update_auto", True)))
        log("自动检查更新 -> %s" % ("开" if self.cfg.get("update_auto", True) else "关"))

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
            "手势绑定: %s" % " / ".join(
                "%s %s" % (HB.gesture_title(g), HB.action_title(g, v))
                for g, v in HB.binds_from_cfg(self.cfg).items()),
            "方向翻转: %s | 增益 %.2g" % (st["flip"], self.cfg["gain"]),
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
