#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mipad DP Touch —— 引擎层 (HID -> 鼠标 / 滚轮)

把 hid_bridge.py 里那套「HID 回调 -> 合成鼠标事件」的状态机封装成一个可启停、
可查询、可运行时改配置的服务对象，供菜单栏界面使用。

设计要点
--------
1. **不复制 hid_bridge 的任何逻辑**：直接 import 复用它的 ctypes 层和 Bridge 类，
   命令行与图形界面走同一条实现路径，不会出现「CLI 好用、GUI 行为不一样」的分裂。
2. IOHIDManager 挂到**自己那条后台线程**的 CFRunLoop 上, 不是 AppKit 的主 run loop。
   原因: 主 run loop 在菜单跟踪 / 模态框 / 拖拽时会被切到别的 mode (kCFRunLoopDefaultMode
   上的源在那些 mode 里收不到事件) —— 表现就是「菜单一打开, 笔就失灵」。
   自己开一条 run loop 之后, 界面上干什么都不再影响笔。
   因为回调不在主线程了, 所有共享状态 (bridge / 计数 / devices) 都走 self._lk。
3. ctypes 回调里绝不向外抛异常 —— 异常会穿过 C 栈直接搞崩进程。一律吞掉并记进
   last_error。
"""

import ctypes
import subprocess
import threading
import time

import hid_bridge as H

KCFSTRING_UTF8 = 0x08000100
XIAOMI_VID = 0x2717

# 已知设备表：(VID, PID, 显示名, 是否已实测验证)
# 优先保证小米平板 9 Pro Max —— 小米首款支持 DP-in 的平板。
# 后续小米若再出 DP-in 机型，在这里加一行即可；表外的设备走「允许未验证设备」开关。
KNOWN_DEVICES = [
    (0x2717, 0x2D05, "小米平板 9 Pro Max", True),
]

MODES = {
    "scroll": "滑动翻页 (笔尖划动 = 滚动, 不选中文字)",
    "select": "滑动选择 (笔尖划动 = 拖拽 / 划选文字)",
}

NATURAL_OPTS = {
    "system": "跟随系统「自然滚动」设置",
    "on": "始终自然 (手指下滑 -> 看后面的内容)",
    "off": "始终传统 (手指下滑 -> 看前面的内容)",
}

# 滚动增益档位。菜单(短标签给窗口下拉用)与 dptouch.py 共用这一份, 别各存一边。
GAINS = [(0.5, "慢"), (0.75, "偏慢"), (1.0, "标准"), (1.5, "快"), (2.0, "很快")]


# --------------------------------------------------------------------------
# ctypes 声明补齐 (hid_bridge 没用到这几个函数)
# --------------------------------------------------------------------------

DEVICE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                             ctypes.c_void_p, ctypes.c_void_p)
CF_AB = lambda: ctypes.addressof(ctypes.c_char.in_dll(H.cf, "kCFTypeArrayCallBacks"))


_INITED = False


def _init_ctypes():
    global _INITED
    if _INITED:
        return
    _INITED = True
    cf, io = H.cf, H.iokit
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                      ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    cf.CFArrayCreate.restype = ctypes.c_void_p
    cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_long, ctypes.c_void_p]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    io.IOHIDManagerSetDeviceMatchingMultiple.restype = None
    io.IOHIDManagerSetDeviceMatchingMultiple.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    io.IOHIDManagerRegisterDeviceMatchingCallback.restype = None
    io.IOHIDManagerRegisterDeviceMatchingCallback.argtypes = [ctypes.c_void_p, DEVICE_CB, ctypes.c_void_p]
    io.IOHIDManagerRegisterDeviceRemovalCallback.restype = None
    io.IOHIDManagerRegisterDeviceRemovalCallback.argtypes = [ctypes.c_void_p, DEVICE_CB, ctypes.c_void_p]
    io.IOHIDManagerClose.restype = None
    io.IOHIDManagerClose.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    io.IOHIDManagerUnscheduleFromRunLoop.restype = None
    io.IOHIDManagerUnscheduleFromRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]



# 导入即完成 ctypes 签名声明 —— 不依赖 Engine 实例化顺序
_init_ctypes()

def _dict(pairs):
    """[(bytes_key, int_val)] -> CFDictionaryRef (调用方负责 CFRelease)"""
    n = len(pairs)
    keys = (ctypes.c_void_p * n)()
    vals = (ctypes.c_void_p * n)()
    for i, (k, v) in enumerate(pairs):
        keys[i] = H.cfstr(k)
        vals[i] = H.cfnum(v)
    return H.cf.CFDictionaryCreate(None, keys, vals, n, H.CF_KB(), H.CF_VB())


def _cfstr_to_str(ref):
    """CFStringRef -> Python str"""
    if not ref:
        return ""
    buf = ctypes.create_string_buffer(512)
    if H.cf.CFStringGetCString(ref, buf, 512, KCFSTRING_UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


PROP_PRODUCT = None
PROP_VID = None
PROP_PID = None


def _init_props():
    global PROP_PRODUCT, PROP_VID, PROP_PID
    if PROP_PRODUCT is None:
        PROP_PRODUCT = H.cfstr(b"Product")
        PROP_VID = H.cfstr(b"VendorID")
        PROP_PID = H.cfstr(b"ProductID")


def device_info(dev):
    """读一个 IOHIDDeviceRef 的 (产品名, VID, PID)"""
    _init_props()
    name = _cfstr_to_str(H.iokit.IOHIDDeviceGetProperty(dev, PROP_PRODUCT))
    vid = pid = 0
    for key, tag in ((PROP_VID, "v"), (PROP_PID, "p")):
        ref = H.iokit.IOHIDDeviceGetProperty(dev, key)
        if ref:
            out = ctypes.c_int32(0)
            if H.cf.CFNumberGetValue(ref, H.SINT32, ctypes.byref(out)):
                if tag == "v":
                    vid = out.value
                else:
                    pid = out.value
    return name, vid, pid


def friendly_name(vid, pid, raw=""):
    for v, p, name, _verified in KNOWN_DEVICES:
        if v == vid and p == pid:
            return name
    if raw:
        return "%s (未验证, %04X:%04X)" % (raw, vid, pid)
    return "未知设备 (%04X:%04X)" % (vid, pid)


# --------------------------------------------------------------------------
# 系统「自然滚动」检测
# --------------------------------------------------------------------------

def system_natural_scrolling():
    """读系统「自然滚动」开关。读不到按关闭 (False) 算 —— 与 defaults 默认一致。

    说明：我们 post 的 CGEvent 滚动量是「最终值」，系统不会再按这个开关反转一次。
    所以这个开关只用来推断**用户习惯的方向**：开了自然滚动的人，期望手指下滑时
    看到后面的内容。跟随它就等于跟随用户直觉。
    """
    try:
        from Foundation import NSUserDefaults
        d = NSUserDefaults.standardUserDefaults().persistentDomainForName_("NSGlobalDomain")
        if d and "com.apple.swipescrolldirection" in d:
            return bool(d["com.apple.swipescrolldirection"])
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["/usr/bin/defaults", "read", "-g", "com.apple.swipescrolldirection"],
            stderr=subprocess.DEVNULL)
        return out.strip() == b"1"
    except Exception:
        return False


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------

class Engine(object):
    def __init__(self, log=None):
        _init_ctypes()
        self.log = log if log is not None else (lambda m: None)
        self.cfg = {
            "mode": "scroll",         # scroll | select
            "natural": "system",      # system | on | off
            "gain": 1.0,              # 滚动增益
            "takeover": False,        # 试验性: 用笔的绝对坐标驱动光标
            "drag_pen": False,        # 拖拽位置源改用笔坐标
            "allow_unknown": False,   # 允许未验证设备
            "hold_drag": True,        # 触屏模式: 笔尖停住再划 = 拖拽 (否则拖不动窗口)
            "hold_ms": 250,           # 长按判定的时长
            "rc_hold_ms": 800,        # ★长按不动这么久 -> 抬手弹右键菜单 (0 = 关)
            "rc_barrel": False,       # 笔侧键 / 橡皮擦端 -> 右键 (要笔硬件真上报才有效)
        }
        # HID 回调跑在自己那条线程上, 和主线程 (菜单 / 定时刷新) 共享状态 -> 一把递归锁。
        # 必须在 _new_bridge 之前就位: _apply() 会读 self.debug。
        self._lk = threading.RLock()
        self.debug = False            # 详细日志开关 (菜单里可切)
        self._th = None               # HID 线程
        self._rl = None               # HID 线程的 CFRunLoop
        self._mode_str = None         # 复用同一个 CFString, 别在热路径里造对象
        self._stop_flag = False
        self._ready = threading.Event()
        self._err_logged = set()      # 记过日志的异常, 免得每帧刷屏
        self.reset_counters()
        self._want_down = False
        self._btn = {}                # BarrelSwitch / Eraser 的上一次状态 (只在按下沿动作)
        self._btn_seen = set()        # 实测见到过的笔按键名 (日志/诊断用)
        self.b = None
        self._new_bridge()
        self.mgr = None
        self.rc = 0
        self.running = False
        self.devices = []             # [(名字, vid, pid, 是否已验证)]
        self.last_error = ""
        self.started_at = 0.0

    # ---------------- 配置 ----------------

    def _new_bridge(self):
        """(重)建 Bridge。只丢中间状态，累计计数由 Engine 自己保管。"""
        self.b = H.Bridge(takeover=False, drag_pen=False, scroll=True, hold_drag=True)
        self.b.say = self.log
        # 构造期先同步一次基准值, 免得日志里先打一行引擎默认值、再打一行真实值 (看着像设置没生效)
        self._rc_log = None        # None = 还没拿到真实配置, 所以构造期那一次不打日志
        self._apply()

    def _apply(self):
        m = self.cfg["mode"]
        self.b.debug = bool(self.debug)
        self.b.takeover = bool(self.cfg["takeover"])
        self.b.scroll = (m == "scroll") and not self.b.takeover
        self.b.drag_pen = bool(self.cfg["drag_pen"])
        # 长按拖拽只在触屏模式下有意义 (滑动选择模式下每一笔本来就是拖拽)
        self.b.hold_drag = bool(self.cfg.get("hold_drag", True)) and self.b.scroll
        self.b.hold_dt = float(self.cfg.get("hold_ms") or 250) / 1000.0
        # 长按不动 = 右键: 只在触屏模式下有意义 (滑动选择模式每一笔本来就有按键语义)
        self.b.rc_hold_dt = (float(self.cfg.get("rc_hold_ms") or 0) / 1000.0) if self.b.scroll else 0.0
        self.b.rc_barrel = bool(self.cfg.get("rc_barrel", False))
        if self._rc_log is not None and self.b.rc_hold_dt != self._rc_log:
            self._rc_log = self.b.rc_hold_dt
            self.log("长按不动 -> %s" % ("%.1f s = 右键菜单 (容差 %d px)"
                                        % (self.b.rc_hold_dt, H.RC_MAX_PX) if self.b.rc_hold_dt
                                        else "关"))
        self.b.gain = float(self.cfg["gain"])
        self.b.scroll_flip = self._flip()
        self._rc_log = self.b.rc_hold_dt

    def _flip(self):
        nat = self.cfg["natural"]
        if nat == "system":
            return system_natural_scrolling()
        return nat == "on"

    def update(self, **kw):
        """运行时改配置, 立即生效 (无需重启桥接)"""
        with self._lk:
            return self._update_locked(kw)

    def _update_locked(self, kw):
        if "debug_log" in kw:
            self.debug = bool(kw["debug_log"])
        before = (self.cfg["mode"], self.cfg["allow_unknown"],
                  bool(self.cfg.get("hold_drag", True)))
        self.cfg.update(kw)
        self._apply()
        if before != (self.cfg["mode"], self.cfg["allow_unknown"],
                      bool(self.cfg.get("hold_drag", True))):
            self.b.reset_state()
        if self.cfg["allow_unknown"] != before[1] and self.mgr is not None:
            return self.restart()      # 匹配条件变了, 必须重建 manager
        return 0

    def set_debug(self, on):
        """详细日志开关 (给菜单用)"""
        with self._lk:
            self.debug = bool(on)
            self.b.debug = bool(on)
        self.log("详细日志 -> %s" % ("开" if self.debug else "关"))
        return self.debug

    def zero_counters(self):
        """清零。桥接的计数也要一起清 —— 主线程和 HID 线程抢的是同一批字段。"""
        with self._lk:
            self.reset_counters()
            b = self.b
            b.n = b.ns = b.nd = b.nclk = b.nrc = 0

    def reset_transient(self):
        """丢掉中间状态 (菜单关掉时调用): 半按 / 划动中的残留不能带到下一次。"""
        with self._lk:
            self._want_down = False
            self.pres_cur = 0
            b = self.b
            b.down = False
            b.scrolled = False
            b.pend = 0.0

    def _log_err_once(self, what, e):
        """同一个异常只写一次日志 (回调每秒可能触发上百次, 不能刷屏)"""
        key = "%s: %r" % (what, e)
        if key in self._err_logged:
            return
        self._err_logged.add(key)
        try:
            import traceback
            self.log("%s: %s\n%s" % (what, key, traceback.format_exc()))
        except Exception:
            pass

    def _dbg_hover_line(self, b):
        try:
            p = H.cg.CGEventGetLocation(H.cg.CGEventCreate(None))
            qx, qy = b.pen_screen()
            return ("笔尖 (%.4f,%.4f) -> 屏 %.0f,%.0f | 光标 @%.0f,%.0f | 差 %.0f,%.0f | 按下=%s"
                    % (b.x, b.y, qx, qy, p.x, p.y, p.x - qx, p.y - qy, b.down))
        except Exception as e:
            return "笔尖自检异常: %r" % (e,)

    def reset_counters(self):
        self.seen = {"ev": 0, "x": 0, "y": 0, "tip": 0, "pres": 0}
        self.pres_cur = 0
        self.pres_peak = 0
        self.total = {"fwd": 0, "scroll": 0, "drag": 0, "click": 0}

    # ---------------- 生命周期 ----------------

    def _match(self):
        dicts = []
        if self.cfg["allow_unknown"]:
            dicts.append(_dict([(b"VendorID", XIAOMI_VID)]))
        else:
            for v, p, _n, _ok in KNOWN_DEVICES:
                dicts.append(_dict([(b"VendorID", v), (b"ProductID", p)]))
        arr = (ctypes.c_void_p * len(dicts))(*dicts)
        ret = H.cf.CFArrayCreate(None, arr, len(dicts), CF_AB())
        for d in dicts:
            H.cf.CFRelease(d)
        return ret

    def start(self):
        """打开 HID。返回 IOHIDManagerOpen 的 rc (0 = 成功, 负值 = 缺权限)。

        manager 建/开/收尾全在**自己那条线程**上完成 (ctypes 回调也在那条线程里跑),
        这样 AppKit 的菜单跟踪 / 模态框 / 拖拽都不会影响笔。
        """
        if self._th is not None and self._th.is_alive():
            return self.rc
        self.last_error = ""
        self._stop_flag = False
        self._ready.clear()
        self._th = threading.Thread(target=self._hid_thread, name="dptouch-hid", daemon=True)
        self._th.start()
        self._ready.wait(5.0)         # 等 open 出结果, 好把 rc 报给界面
        return self.rc

    def _hid_thread(self):
        """HID 线程: 建 manager -> 开 -> 跑自己的 run loop -> 收尾。"""
        try:
            self.mgr = H.iokit.IOHIDManagerCreate(None, 0)
            H.iokit.IOHIDManagerSetDeviceMatchingMultiple(self.mgr, self._match())
            self._cb = self._value_cb()
            self._dev_cb = self._device_cb(True)
            self._rm_cb = self._device_cb(False)
            H.KEEP.extend([self._cb, self._dev_cb, self._rm_cb])
            H.iokit.IOHIDManagerRegisterInputValueCallback(self.mgr, self._cb, None)
            H.iokit.IOHIDManagerRegisterDeviceMatchingCallback(self.mgr, self._dev_cb, None)
            H.iokit.IOHIDManagerRegisterDeviceRemovalCallback(self.mgr, self._rm_cb, None)
            self._rl = H.cf.CFRunLoopGetCurrent()
            self._mode_str = H.cfstr(H.DEFAULT_MODE)
            H.iokit.IOHIDManagerScheduleWithRunLoop(self.mgr, self._rl, self._mode_str)
            self.rc = H.iokit.IOHIDManagerOpen(self.mgr, 0)
            self.running = (self.rc == 0)
            self.started_at = time.time()
            self.log("HID 线程已就绪 (独立 run loop, 界面操作不会影响它)")
        except Exception as e:
            self.last_error = "启动失败: %r" % (e,)
            self._log_err_once("HID 启动失败", e)
            self.rc = -1
            self.running = False
        finally:
            self._ready.set()
        try:
            while not self._stop_flag:
                H.cf.CFRunLoopRunInMode(self._mode_str, 0.2, False)
        except Exception as e:
            self.last_error = "HID 线程异常: %r" % (e,)
            self._log_err_once("HID 线程异常", e)
        finally:
            try:
                if self.mgr is not None:
                    H.iokit.IOHIDManagerClose(self.mgr, 0)
                    H.iokit.IOHIDManagerUnscheduleFromRunLoop(self.mgr, self._rl,
                                                              self._mode_str)
                    H.cf.CFRelease(self.mgr)
            except Exception as e:
                self._log_err_once("HID 收尾异常", e)
            self.mgr = None
            self.running = False
            self._rl = None

    def stop(self):
        self._stop_flag = True
        if self._rl:
            try:
                H.cf.CFRunLoopStop(self._rl)      # 立刻打断 RunInMode, 不用等 0.2s 超时
            except Exception:
                pass
        th, self._th = self._th, None
        if th is not None and th.is_alive():
            th.join(3.0)
        with self._lk:
            if self.b is not None:
                self.b.reset_state()
            self.devices = []

    def restart(self):
        self.stop()
        return self.start()

    def resync_display(self):
        """显示模式变了 (分辨率 / HiDPI 缩放) -> 重建 Bridge, 让绝对坐标按新的
        CGDisplayBounds 归一化。只换 Bridge 对象, 不碰 IOHIDManager, 笔桥接不中断。"""
        with self._lk:
            self._new_bridge()
            w, h = self.b.w, self.b.h
        self.log("坐标范围已更新: %.0fx%.0f" % (w, h))
        return (w, h)

    def ax_trusted(self):
        try:
            return bool(H.ax.AXIsProcessTrusted())
        except Exception:
            return False

    # ---------------- 回调 ----------------

    def _device_cb(self, added):
        eng = self

        @DEVICE_CB
        def cb(ctx, result, sender, device):
            try:
                raw, vid, pid = device_info(device)
                name = friendly_name(vid, pid, raw)
                known = any(v == vid and p == pid for v, p, _n, _o in KNOWN_DEVICES)
                if added:
                    with eng._lk:
                        if not any(d[1] == vid and d[2] == pid for d in eng.devices):
                            eng.devices.append((name, vid, pid, known))
                    eng.log("设备接入: %s" % name)
                else:
                    with eng._lk:
                        eng.devices = [d for d in eng.devices
                                       if not (d[1] == vid and d[2] == pid)]
                    eng.log("设备移除: %s" % name)
            except Exception as e:
                eng.last_error = "设备回调异常: %r" % (e,)
                eng._log_err_once("设备回调异常", e)
        return cb

    def _value_cb(self):
        eng = self

        @H.CALLBACK
        def cb(ctx, result, sender, value):
            try:
                eng.on_value(sender, value)
            except Exception as e:
                # 绝不能把异常抛回 C 栈
                eng.last_error = "事件回调异常: %r" % (e,)
                eng._log_err_once("事件回调异常", e)
        return cb

    def on_value(self, sender, value):
        """ctypes 回调入口: 只做 IOHID 解析, 逻辑全在 feed()"""
        if not value:
            return
        el = H.iokit.IOHIDValueGetElement(value)
        if not el:
            return
        self.feed(H.iokit.IOHIDElementGetUsagePage(el),
                  H.iokit.IOHIDElementGetUsage(el),
                  H.iokit.IOHIDValueGetIntegerValue(value),
                  H.iokit.IOHIDElementGetLogicalMin(el),
                  H.iokit.IOHIDElementGetLogicalMax(el))

    def feed(self, page, usage, v, lo, hi):
        """一个 HID 元素的值变化。与 IOHID 解析解耦, 便于离线单测。"""
        with self._lk:
            self._feed_locked(page, usage, v, lo, hi)

    def _feed_locked(self, page, usage, v, lo, hi):
        b = self.b
        if page == 0x0D and usage == 0x42:                 # TipSwitch
            self.seen["ev"] += 1
            self.seen["tip"] += 1
            self._want_down = bool(v)
            self._sync()
        elif page == 0x0D and usage == 0x30:               # TipPressure
            self.seen["ev"] += 1
            self.seen["pres"] += 1
            self.pres_cur = v
            if v > self.pres_peak:
                self.pres_peak = v
            self._sync()
        elif page == 0x0D and usage == 0x32:               # InRange
            if not v:
                self._want_down = False
                self.pres_cur = 0
                self._sync()
        elif page == 0x0D and usage in (0x44, 0x45):       # BarrelSwitch / Eraser
            # 笔侧键 / 橡皮擦端。macOS 不认这个小工具, 所以要靠我们自己翻成右键。
            # 这条日志【恒定输出】(不走 debug): 用来确认这支笔到底报不报侧键。
            self.seen["ev"] += 1
            was = self._btn.get(usage, 0)
            self._btn[usage] = 1 if v else 0
            if v and not was:
                nm = "笔侧键" if usage == 0x44 else "橡皮擦端"
                self._btn_seen.add(nm)
                on = bool(self.b.rc_barrel)
                self.log("笔按键: %s 按下%s" % (nm, " -> 右键" if on else " (未映射)"))
                if on:
                    self.b.right_click()
        elif page == 0x01 and usage in (0x30, 0x31) and hi > lo and lo >= 0:
            # 绝对坐标 (笔接口); 相对坐标的鼠标接口 lo < 0, 会被排除
            self.seen["ev"] += 1
            self.seen["x" if usage == 0x30 else "y"] += 1
            nrm = float(v - lo) / (hi - lo)
            if usage == 0x30:
                b.x = nrm
            else:
                b.y = nrm
            if b.takeover:
                b.post(H.LDRAGGED if b.down else H.MOVED)
            elif b.down and b.scroll:
                b.on_move()
            elif b.down:
                b.drag()
            if self.debug:
                # 每 0.4 秒一条: 笔尖位置 vs 系统光标。光标没跟着笔走时一眼看出来。
                b.dbg(self._dbg_hover_line(b), min_dt=0.4)
        self.total["fwd"] = b.n
        self.total["scroll"] = b.ns
        self.total["drag"] = b.nd
        self.total["click"] = b.nclk

    def _sync(self):
        """TipSwitch / 压力 / InRange 变化后统一处理按下与抬起"""
        b = self.b
        want = getattr(self, "_want_down", False)
        if want == b.down:
            return
        if want:
            b.press()
        else:
            if not b.scroll:
                b.last_drag = 0.0     # 抬起前补发最后一段拖拽
                b.drag()
            b.release()

    # ---------------- 状态 ----------------

    def status(self):
        with self._lk:
            return self._status_locked()

    def _status_locked(self):
        return {
            "running": self.running,
            "rc": self.rc,
            "ax": self.ax_trusted(),
            "devices": list(self.devices),
            "events": self.seen["ev"],
            "fwd": self.b.n if self.b else 0,
            "scroll": self.b.ns if self.b else 0,
            "click": self.b.nclk if self.b else 0,
            "drag": self.b.nd if self.b else 0,
            "rclick": self.b.nrc if self.b else 0,
            "down": bool(self.b.down) if self.b else False,
            "mode": self.cfg["mode"],
            "flip": bool(self.b.scroll_flip) if self.b else False,
            "error": self.last_error,
        }

    def status_line(self):
        st = self.status()
        if not st["running"]:
            if st["rc"] < 0:
                return "未运行 (HID 打不开: rc=%d, 缺「输入监控」)" % st["rc"]
            return "已暂停"
        if not st["devices"]:
            return "已开启 · 等平板上线"
        names = "、".join(d[0] for d in st["devices"])
        tail = "%d 滚动 / %d 点击" % (st["scroll"], st["click"]) \
            if st["mode"] == "scroll" else "%d 拖拽 / %d 点击" % (st["drag"], st["click"])
        if st.get("rclick"):
            tail += " / %d 右键" % st["rclick"]
        return "已连接 · %s · %s" % (names, tail)

    def status_line_short(self):
        """菜单栏第一行。不带括号、也不解释原因 —— 原因在设置窗口的权限那一段里说。"""
        st = self.status()
        if not st["running"]:
            return "已暂停" if st["rc"] >= 0 else "未运行 · 缺少输入监控权限"
        if not st["devices"]:
            return "已开启 · 等平板上线"
        names = "、".join(d[0] for d in st["devices"])
        tail = "%d 滚动 / %d 点击" % (st["scroll"], st["click"]) \
            if st["mode"] == "scroll" else "%d 拖拽 / %d 点击" % (st["drag"], st["click"])
        if st.get("rclick"):
            tail += " / %d 右键" % st["rclick"]
        return "已连接 · %s · %s" % (names, tail)
