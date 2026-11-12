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

import dptouch_display as DP
import hid_bridge as H

KCFSTRING_UTF8 = 0x08000100

# ---------------------------------------------------------------------------
# 已知设备判据（三条同时成立才认）
#   ① VendorID ∈ SUPPORTED_VENDOR_IDS   ② ProductID == PRODUCT_ID
#   ③ 接口 = 笔集合(UsagePage 0x0D / Usage 0x02)   另外产品名做二次核对
#
# ★ 为什么是**两个** VID：平板开 USB 调试后 gadget 身份会从小米 0x2717 切到
#   Google/Android 通用 0x18D1。2026-09-17 实测：写死 0x2717 时命中 0 个接口 ——
#   平板侧 HID 完好、macOS 也挂上了笔，App 一个都打不开，表现为菜单停在
#   「等待平板上线」+ 笔全失效。
# ★ 为什么不能只匹配 0x18D1：0x18D1 是通用 Android 身份，别的 Android 设备
#   也是它。VID 白名单 + PID + 接口 usage 三道门一起上，普通设备进不来。
# ★★ 绝不要把 0x05AC 混进来：0x05AC(Apple) 是**平板看主机**的 DP Alt Mode SVID
#   （usb_dp_relay 用它判 is_mac=1 并据此只建 Pen-only 描述符），
#   与「主机看平板」的 VID 是两个方向、两件事。
SUPPORTED_VENDOR_IDS = (0x2717, 0x18D1)   # 小米身份 / Google 通用身份
PRODUCT_ID = 0x2D05                       # Xiaomi Pad 9 Pro Max
PEN_USAGE_PAGE = 0x0D                     # Digitizer
PEN_USAGE = 0x02                          # Pen
TOUCH_USAGE = 0x04                        # Touch Screen（平板侧解锁后 hid.usb1 变成它）
PRODUCT_NAME = "Xiaomi Pad 9 Pro Max"

# 「触摸+笔」二合一描述符（914B）里的 report ID 分工：
#   1 = 10 根手指（每指: TipSwitch 0x42 / InRange 0x32 / ContactID 0x51 / X / Y）
#   2 = 接触计数 (0x54 输入, 0x55 feature)
#   3 = 笔（与纯笔接口逐字节相同 —— 所以笔的解析逻辑一个字都不用改）
TOUCH_REPORT_ID = 1
# 同一份 report 里 X/Y 是连着回调的(微秒级); 隔了半拍才到的另一轴是上一次 report 的残值。
TOUCH_PAIR_STALE_S = 0.004
PEN_REPORT_ID = 3

# 兼容旧名（探针脚本在用）
TABLET_VIDS = SUPPORTED_VENDOR_IDS
TABLET_PID = PRODUCT_ID

# 已知设备表：(PID, 显示名, 是否已实测验证)
# 后续小米若再出 DP-in 机型，在这里加一行即可；表外设备走「允许未验证设备」开关。
KNOWN_DEVICES = [
    (0x2D05, "小米平板 9 Pro Max", True),
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
    # 元素枚举：用来找出「触摸+笔」接口里第一根手指的那几个元素（见 first_finger_cookies）
    io.IOHIDDeviceCopyMatchingElements.restype = ctypes.c_void_p
    io.IOHIDDeviceCopyMatchingElements.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    io.IOHIDElementGetCookie.restype = ctypes.c_uint32
    io.IOHIDElementGetCookie.argtypes = [ctypes.c_void_p]
    io.IOHIDElementGetReportID.restype = ctypes.c_uint32
    io.IOHIDElementGetReportID.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]



# 导入即完成 ctypes 签名声明 —— 不依赖 Engine 实例化顺序
_init_ctypes()


def first_finger_cookies_from(rows):
    """从元素表 [(cookie, report_id, page, usage)] 里挑出「第 1 根手指」的 cookie 集合。

    实测（2026-10-14, 914B 描述符）元素在报告里的排列**不是**「一根手指一组」，
    而是按用途分组：
        cookie 22..41   第 1..10 指的 TipSwitch/InRange 对 (42,32,42,32,…)
        cookie 42..45   笔的 TipSwitch/Barrel/Eraser/InRange
        cookie 46..     第 1..10 指的 [ContactID, X, Y] 三元组
    所以不能按「cookie 最小的 1/10」推 —— 那会取到第 1 指的 TipSwitch/InRange
    加第 2 指的 TipSwitch/InRange，而第 1 指的 X/Y 反而被挡掉（表现为：手指点击
    落在 (0,0)、光标不动）。
    正确做法：第 1 指的 TipSwitch / InRange 各取第一个；ContactID 取第一个 0x51；
    X / Y 取 cookie 紧跟在那个 ContactID 之后的两个。
    """
    touch = [r for r in rows if r[1] == TOUCH_REPORT_ID]
    if not touch:
        return set()
    out = set()
    for usage in (0x42, 0x32):                       # TipSwitch / InRange: 各取第一项
        for ck, _rid, pg, u in touch:
            if pg == 0x0D and u == usage:
                out.add(ck)
                break
    ids = [r for r in touch if r[2] == 0x0D and r[3] == 0x51]
    if ids:
        cid = ids[0][0]
        out.add(cid)
        for usage in (0x30, 0x31):                   # X / Y: 紧跟首个 ContactID 之后
            cand = [r for r in touch
                    if r[2] == 0x01 and r[3] == usage and r[0] > cid]
            if cand:
                out.add(min(cand, key=lambda r: r[0])[0])
    return out


def first_finger_cookies(dev):
    """「触摸+笔」接口里第一根手指占用的元素 cookie 集合（纯笔接口返回空集）。"""
    try:
        arr = H.iokit.IOHIDDeviceCopyMatchingElements(dev, None, 0)
    except Exception:
        return set()
    if not arr:
        return set()
    rows = []
    try:
        n = H.cf.CFArrayGetCount(arr)
        for i in range(n):
            el = H.cf.CFArrayGetValueAtIndex(arr, i)
            if not el:
                continue
            rows.append((H.iokit.IOHIDElementGetCookie(el),
                         H.iokit.IOHIDElementGetReportID(el),
                         H.iokit.IOHIDElementGetUsagePage(el),
                         H.iokit.IOHIDElementGetUsage(el)))
    finally:
        H.cf.CFRelease(arr)
    rows.sort()
    return first_finger_cookies_from(rows)

def finger_cookie_map_from(rows):
    """每根手指的 cookie: tip / inr / cid / x / y（触摸接口，report ID 1）。"""
    touch = [r for r in rows if r[1] == TOUCH_REPORT_ID]
    tips = [r[0] for r in touch if r[2] == 0x0D and r[3] == 0x42]
    inrs = [r[0] for r in touch if r[2] == 0x0D and r[3] == 0x32]
    cids = [r[0] for r in touch if r[2] == 0x0D and r[3] == 0x51]
    xs = [r[0] for r in touch if r[2] == 0x01 and r[3] == 0x30]
    ys = [r[0] for r in touch if r[2] == 0x01 and r[3] == 0x31]
    out = []
    for i, cid in enumerate(cids):
        if i >= len(tips) or i >= len(inrs):
            break
        x = min([c for c in xs if c > cid], default=None)
        y = min([c for c in ys if c > cid], default=None)
        if x is None or y is None:
            break
        out.append({"tip": tips[i], "inr": inrs[i], "cid": cid, "x": x, "y": y})
    return out


def finger_cookie_map(dev):
    """触摸接口的全部手指 cookie 表 (纯笔接口返回 [])。"""
    try:
        arr = H.iokit.IOHIDDeviceCopyMatchingElements(dev, None, 0)
    except Exception:
        return []
    if not arr:
        return []
    rows = []
    try:
        n = H.cf.CFArrayGetCount(arr)
        for i in range(n):
            el = H.cf.CFArrayGetValueAtIndex(arr, i)
            if el:
                rows.append((H.iokit.IOHIDElementGetCookie(el),
                             H.iokit.IOHIDElementGetReportID(el),
                             H.iokit.IOHIDElementGetUsagePage(el),
                             H.iokit.IOHIDElementGetUsage(el)))
    finally:
        H.cf.CFRelease(arr)
    rows.sort()
    return finger_cookie_map_from(rows)


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


def is_tablet(vid, pid, raw=""):
    """是不是本项目的平板：VID 白名单 + ProductID + 产品名（读到才核对）。

    三个条件一起上，是为了既认得出「平板开 USB 调试后换成 0x18D1」这一轮，
    又不把机器上别的 Android 设备（同样是 0x18D1）收进来。
    """
    if vid not in SUPPORTED_VENDOR_IDS or pid != PRODUCT_ID:
        return False
    if raw and raw.strip() and PRODUCT_NAME.lower() not in raw.lower():
        return False
    return True


def friendly_name(vid, pid, raw=""):
    """能认出就报中文名；认不出报原始产品名 + 身份，方便贴日志排查。"""
    if is_tablet(vid, pid, raw):
        for p, name, _verified in KNOWN_DEVICES:
            if p == pid:
                return name
        return PRODUCT_NAME
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


def counters_tail(st):
    """状态行的计数尾巴。

    报什么由**绑定表**决定: 有手势绑了「滚动」就报滚动, 否则报拖拽 —— 以前是按
    全局模式报的, 现在模式没了, 表就是唯一判据。
    """
    used = set((st.get("binds") or {}).values())
    if "scroll" in used:
        tail = "%d 滚动 / %d 点击" % (st["scroll"], st["click"])
    elif "drag" in used:
        tail = "%d 拖拽 / %d 点击" % (st["drag"], st["click"])
    else:
        tail = "%d 点击" % st["click"]
    if st.get("rclick"):
        tail += " / %d 右键" % st["rclick"]
    return tail


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------

class Engine(object):
    def __init__(self, log=None):
        _init_ctypes()
        self.log = log if log is not None else (lambda m: None)
        self.cfg = {
            "natural": "system",      # system | on | off
            "gain": 1.0,              # 滚动增益
            "target_display": DP.TARGET_AUTO,   # 光标基准屏: auto | tablet | "<displayID>"
            "drag_pen": False,        # 拖拽位置源改用笔坐标
            "allow_unknown": False,   # 允许未验证设备
            "input_mode": "auto",      # auto | pen | touch —— 认哪种 HID 接口（见 H.INPUT_MODES）
                                       # 默认 auto = 0x02 笔 / 0x04 触摸+笔 两种都认:
                                       # 没解锁的平板只给笔那一路, 行为与 1.1 一字不差;
                                       # 解锁过的平板开箱即用（不用动任何开关）。
            "display_allow_unknown": False,     # 允许把未实测的屏当平板屏
            "touch_mode": "auto",
            "legacy_touch_ack": False,
            "native_touch": False,               # effective compatibility mirror, never preference
                                                 # (macOS 27.3+ 系统直接处理手指, 桥接只服务笔)
            "hold_ms": 250,           # 「停住再滑」的判定时长
            "rc_hold_ms": 800,        # 「停住不动」的判定时长 (0 = 关)
        }
        # 手势 -> 动作 的绑定。唯一出处是 hid_bridge 顶部那张 BIND_GESTURES 表,
        # 这里只把默认值摊平成配置键 (bind_tap / bind_swipe / bind_hold_swipe / bind_hold)。
        for _g, _a in H.DEFAULT_BINDS.items():
            self.cfg[H.bind_key(_g)] = _a
        # HID 回调跑在自己那条线程上, 和主线程 (菜单 / 定时刷新) 共享状态 -> 一把递归锁。
        # 必须在 _new_bridge 之前就位: _apply() 会读 self.debug。
        self._lk = threading.RLock()
        # 「切换屏幕」是在 HID 线程里切的 (笔一按就得换, 不能等主线程那一拍), 但落盘和
        # 菜单打勾只能在主线程做 -> 这里留一个「待办」, 由 App 的定时器取走 (见 next_screen)。
        self.pending_target = None
        self.debug = False            # 详细日志开关 (菜单里可切)
        self._th = None               # HID 线程
        self._rl = None               # HID 线程的 CFRunLoop
        self._mode_str = None         # 复用同一个 CFString, 别在热路径里造对象
        self._stop_flag = False
        self._ready = threading.Event()
        self._err_logged = set()      # 记过日志的异常, 免得每帧刷屏
        self.reset_counters()
        self._want_down = False
        self._touch_cookies = set()   # 「触摸+笔」接口里第 1 根手指的元素 cookie
        self._finger_map = []         # 全部触点的 cookie 表 (双指滚动用)
        self._cookie2finger = {}      # cookie -> (手指序号, 类型)
        self._fstate = []             # 每根手指: down / x / y
        # 触控可视化: 只读快照, 面板打开时才更新, 不参与任何合成。
        self._live_on = False
        self._live_lk = threading.Lock()
        self._live_fingers = []
        self._live_pen = {"x": None, "y": None, "px": None, "py": None,
                          "down": False, "inr": False, "pres": 0, "t": 0.0}
        self._live_jumps = 0
        self._multi = False           # 是否正在双指手势
        self._two_prev = None         # 双指质心 (归一化)
        self._two_pend = [0.0, 0.0]   # 待发送的滚动量 (px)
        self._two_t = 0.0
        self._latch_until = 0.0     # 双指结束后短暂吞掉单指事件
        self._pinch_prev = None      # 上一帧两指距离 (px)
        self._pinch_pend = 0.0       # 待发送的缩放量 (滚轮 px)
        self._pinch_active = False   # 是否已发出 began
        self._pinch_acc = 0.0        # 起手累计, 超过阈值才认定为捏合
        self._pinch_native_ok = True # 原生 magnify 是否可用
        self._pinch_fb = 0.0         # 回退档位的累计
        self._pinch_last_pt = (0.0, 0.0)
        self._pinch_n = 0
        self._pinch_dist_s = None     # 平滑后的两指距离
        self._pinch_anchor = (0.0, 0.0)  # 捏合锚点 (开始时的质心)
        self._pinch_t = 0.0          # 上一次触发缩放的时刻
        self._tsx = self._tsy = None  # 触摸坐标缓冲（固件顺序: 坐标先到, 按下标志最后到）
        self._tx_t = self._ty_t = 0.0 # 两轴各自到达的时刻（判断是不是同一份 report, 见 _touch_stage）
        self.touch_iface = False      # 当前插着的平板是否带「触摸屏」接口（= 平板侧解锁过）
        self._touch_devices = set()
        self._mode = self._resolve_mode()
        self.target = {}              # 光标基准屏 (笔的绝对坐标铺到哪块屏上)
        self._tgt_key = None
        self.b = None
        self._new_bridge()
        self.mgr = None
        self.rc = 0
        self.running = False
        self.devices = []             # [(名字, vid, pid, 是否已验证)]
        self._device_records = {}     # per IOHIDDeviceRef, not shared VID/PID
        self.last_error = ""
        self.started_at = 0.0

    # ---------------- 配置 ----------------

    def _new_bridge(self):
        """(重)建 Bridge。只丢中间状态，累计计数由 Engine 自己保管。"""
        self.b = H.Bridge(takeover=False, drag_pen=False, binds=H.DEFAULT_BINDS)
        self.b.say = self.log
        self.b.on_switch_screen = self.next_screen      # 绑定动作「切换屏幕」的落点
        # 构造期先同步一次基准值, 免得日志里先打一行引擎默认值、再打一行真实值 (看着像设置没生效)
        self._rc_log = None        # None = 还没拿到真实配置, 所以构造期那一次不打日志
        self._apply()

    def _apply(self):
        binds = H.binds_from_cfg(self.cfg)
        self.b.debug = bool(self.debug)
        self._sync_target()
        self.b.drag_pen = bool(self.cfg["drag_pen"])
        self.b.set_binds(binds)
        self.b.hold_dt = float(self.cfg.get("hold_ms") or 250) / 1000.0
        self.b.set_rc_hold_ms(self.cfg.get("rc_hold_ms") or 0)
        if self._rc_log is not None and self.b.rc_hold_dt != self._rc_log:
            self._rc_log = self.b.rc_hold_dt
            self.log("停住不动 -> %s" % ("%.1f s 判定 (容差 %d px)"
                                        % (self.b.rc_hold_dt, H.RC_MAX_PX) if self.b.rc_hold_dt
                                        else "关"))
        self.b.gain = float(self.cfg["gain"])
        self.b.scroll_flip = self._flip()
        self._rc_log = self.b.rc_hold_dt

    def _sync_target(self, infos=None):
        """按配置算出「光标基准屏」并下发给 Bridge。

        笔报的是归一化绝对坐标, 乘进哪块屏的矩形, 光标就只能在**那块屏**里动 —— 这
        正是「接了第二块屏, 笔跨不过去」的全部原因。所以基准屏是可选项:
          · 跟随光标: 不接管坐标 (笔不移动光标, 只在光标所在那块屏上点/拖/滚)
          · 平板 / 指定某块屏: 接管坐标, 绝对映射铺到那块屏上 (笔能把光标带过去)
        """
        r = DP.resolve_target(self.cfg.get("target_display"),
                              infos=infos,
                              allow_unknown=bool(self.cfg.get("display_allow_unknown")))
        self.target = r
        if self.target_changed(r):
            self.log("光标基准屏 -> %s%s" % (
                r["label"], ("  (接管坐标: 绝对的, 笔到哪光标到哪)" if r["takeover"] else "")))
        if r.get("note"):
            self.log(r["note"])
        rect = r.get("rect")
        if rect:
            self.b.set_rect(rect[0], rect[1], rect[2], rect[3], r["label"])
        self.b.takeover = bool(r["takeover"])
        return r

    def target_changed(self, r):
        """和上次下发的是不是同一块屏 (只影响日志, 不影响行为)"""
        key = (r.get("value"), r.get("takeover"), tuple(r.get("rect") or ()))
        if key != getattr(self, "_tgt_key", None):
            self._tgt_key = key
            return True
        return False

    # ---------------- 「切换屏幕」绑定动作 ----------------

    def next_screen(self):
        """基准屏按顺序切到**下一块** (到头绕回第一块)。成功返回新值, 否则 None。

        跑在 HID 线程上, 所以这里只做「立刻生效」的那一半: 换矩形 + 接管标志 —— 笔的
        下一笔就已经在新那块屏上了。配置落盘 / 菜单打勾交给主线程的 take_pending_target。

        几块屏就按几块屏转; 只有一块屏则什么都不做 (宁可不响应, 也不切到不存在的地方)。
        """
        try:
            with self._lk:
                infos = DP.displays()
                value, why = DP.next_target(
                    self.cfg.get("target_display"), infos=infos,
                    allow_unknown=bool(self.cfg.get("display_allow_unknown")),
                    cursor=DP.cursor_point())
                if not value:
                    self.log("切换屏幕: %s" % why)
                    return None
                self.cfg["target_display"] = value
                r = self._sync_target(infos)
                self.pending_target = value
                self.log("切换屏幕 -> %s" % (r.get("label") or value))
                return value
        except Exception as e:
            # 切屏失败绝不能把这一笔的其它事件也带崩 (HID 回调里抛异常会被静默吞掉)
            self.log("切换屏幕失败: %r" % (e,))
            return None

    def take_pending_target(self):
        """主线程取走「笔上切换屏幕」的结果 (取完即清)。"""
        with self._lk:
            v = self.pending_target
            self.pending_target = None
            return v

    def _flip(self):
        nat = self.cfg["natural"]
        if nat == "system":
            return system_natural_scrolling()
        return nat == "on"

    def update(self, **kw):
        """运行时改配置, 立即生效 (无需重启桥接)"""
        with self._lk:
            return self._update_locked(kw)

    def _resolve_mode(self):
        from dptouch_mode import resolve
        return resolve(self.cfg.get("touch_mode", "auto"), self.touch_iface,
                       legacy_ack=self.cfg.get("legacy_touch_ack", False))

    def _update_locked(self, kw):
        before_mode = self._mode["selected_mode"]
        if "debug_log" in kw:
            self.debug = bool(kw["debug_log"])
        before = (tuple(sorted(H.binds_from_cfg(self.cfg).items())),
                  self.cfg["allow_unknown"], bool(self.cfg.get("drag_pen")),
                  self.cfg.get("input_mode", "auto"),
                  self._mode["native_touch"])
        self.cfg.update(kw)
        self._mode = self._resolve_mode()
        self.cfg["native_touch"] = self._mode["native_touch"]
        self._apply()
        after = (tuple(sorted(H.binds_from_cfg(self.cfg).items())),
                 self.cfg["allow_unknown"], bool(self.cfg.get("drag_pen")),
                 self.cfg.get("input_mode", "auto"),
                 self._mode["native_touch"])
        if self._mode["selected_mode"] != before_mode and self.b.src_touch:
            # End only synthetic gestures already begun, before clearing state.
            # This is not a new click or a native touch-up.
            if self.b.src_touch and self.b.dragging:
                self.b.post(H.LUP)
            if self._pinch_active and self._pinch_native_ok:
                self.b.pinch_native(0.0, 4, *self._pinch_last_pt)
            self._fstate = [{"down": False, "x": None, "y": None} for _ in self._fstate]
            self._touch_clear()
            self._multi = False
            self._two_prev = None
            self._two_pend = [0.0, 0.0]
            self._latch_until = 0.0
            self._pinch_prev = None
            self._pinch_acc = self._pinch_pend = self._pinch_fb = 0.0
            self._pinch_dist_s = None
            self._pinch_active = False
            self._want_down = False
            self.b.reset_state()
        if before[:4] != after[:4] or (before != after and self.b.src_touch):
            self.b.reset_state()      # 绑定变了: 残留不能带到下一笔
        if (after[1] != before[1] or after[3] != before[3]) and self.mgr is not None:
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
            return ("笔尖 (%.4f,%.4f) -> 屏 %.0f,%.0f | 基准 %s%s | 光标 @%.0f,%.0f"
                    " | 差 %.0f,%.0f | 按下=%s"
                    % (b.x, b.y, qx, qy, self.target.get("label", "?"),
                       "(接管坐标)" if b.takeover else "(不接管: 差 无意义)",
                       p.x, p.y, p.x - qx, p.y - qy, b.down))
        except Exception as e:
            return "笔尖自检异常: %r" % (e,)

    def reset_counters(self):
        self.seen = {"ev": 0, "x": 0, "y": 0, "tip": 0, "pres": 0}
        self.pres_cur = 0
        self.pres_peak = 0
        self.total = {"fwd": 0, "scroll": 0, "drag": 0, "click": 0}

    # ---------------- 生命周期 ----------------

    def _match(self):
        """IOHIDManager 匹配表。

        默认（严格）：VID 白名单 × ProductID × digitizer 接口 usage，三个条件同时成立。
          平板开 USB 调试后身份是 0x18D1，写死 0x2717 会一个也匹配不到；
          但只按 0x18D1 匹配又会撞上机器上别的 Android 设备 —— 所以一起上。
        usage 认哪几个由 input_mode 决定（auto 模式下 0x02 笔 / 0x04 触摸+笔 都认；
        没做平板侧解锁的用户只会出现 0x02，所以 auto 与他们原来的行为完全一致）。
        「允许未验证设备」时退化成 VID 白名单（给未验证机型 / 接口布局不同的机型留门）。
        """
        dicts = []
        mode = self.cfg.get("input_mode", "auto")
        if self.cfg["allow_unknown"]:
            for v in SUPPORTED_VENDOR_IDS:
                dicts.append(_dict([(b"VendorID", v)]))
        else:
            for v in SUPPORTED_VENDOR_IDS:
                for usage in H.match_usages(mode):
                    dicts.append(_dict([(b"VendorID", v), (b"ProductID", PRODUCT_ID),
                                        (b"PrimaryUsagePage", PEN_USAGE_PAGE),
                                        (b"PrimaryUsage", usage)]))
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
            _opts = 1 if self.cfg.get("seize_pen") else 0   # 1 = kIOHIDOptionsTypeSeizeDevice
            self.rc = H.iokit.IOHIDManagerOpen(self.mgr, _opts)
            if _opts:
                self.log("HID 独占模式 (seize) rc=%d" % self.rc)
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
            self.devices = []
            self._device_records.clear()
            self._touch_devices.clear()
            self.touch_iface = False
            self._touch_cookies.clear()
            self._cookie2finger.clear()
            self._finger_map = []
            self._fstate = []
            self._update_locked({})
            if self.b is not None:
                self.b.reset_state()

    def restart(self):
        self.stop()
        return self.start()

    def resync_display(self):
        """显示模式变了 (分辨率 / HiDPI 缩放) -> 重建 Bridge, 让绝对坐标按新的
        CGDisplayBounds 归一化。只换 Bridge 对象, 不碰 IOHIDManager, 笔桥接不中断。"""
        with self._lk:
            self._new_bridge()
            w, h = self.b.w, self.b.h
        self.log("坐标范围已更新: %.0fx%.0f (基准屏 %s)"
                 % (w, h, self.target.get("label", "?")))
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
                known = is_tablet(vid, pid, raw)
                if added:
                    with eng._lk:
                        eng._device_records[int(device)] = (name, vid, pid, known)
                        eng.devices = list(dict.fromkeys(eng._device_records.values()))
                    eng.log("设备接入: %s" % name)
                    ck = first_finger_cookies(device)
                    if ck:
                        with eng._lk:
                            eng._touch_cookies |= ck
                            eng.touch_iface = True
                            eng._touch_devices.add(int(device))
                            eng._update_locked({})
                        eng.log("触控接口已识别: 第 1 根手指 %d 个元素 (report ID %d): cookie %s"
                                % (len(ck), TOUCH_REPORT_ID,
                                   ",".join(str(c) for c in sorted(ck))))
                    fm = finger_cookie_map(device)
                    if fm:
                        c2f = {}
                        for i, f in enumerate(fm):
                            for kind in ("tip", "inr", "cid", "x", "y"):
                                if f.get(kind):
                                    c2f[f[kind]] = (i, kind)
                        with eng._lk:
                            eng._finger_map = fm
                            eng._cookie2finger = c2f
                            eng._fstate = [{"down": False, "x": None, "y": None}
                                           for _ in fm]
                        eng.log("触控接口: 解析到 %d 个触点槽" % len(fm))
                else:
                    with eng._lk:
                        eng._device_records.pop(int(device), None)
                        eng.devices = list(dict.fromkeys(eng._device_records.values()))
                        eng._touch_devices.discard(int(device))
                        eng.touch_iface = bool(eng._touch_devices)
                        if not eng.touch_iface:
                            eng._touch_cookies.clear()
                            eng._cookie2finger.clear()
                            eng._finger_map = []
                            eng._fstate = []
                        eng._update_locked({})
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
                  H.iokit.IOHIDElementGetLogicalMax(el),
                  H.iokit.IOHIDElementGetReportID(el),
                  H.iokit.IOHIDElementGetCookie(el))

    def feed(self, page, usage, v, lo, hi, rid=PEN_REPORT_ID, cookie=0):
        """一个 HID 元素的值变化。与 IOHID 解析解耦, 便于离线单测。

        rid/cookie 只在「触摸+笔」接口上有意义：纯笔接口全部是 report ID 3，
        传默认值即可（单测里也只传前 5 个参数）。
        """
        with self._lk:
            self._feed_locked(page, usage, v, lo, hi, rid, cookie)

    # ---- 触摸坐标缓冲 ----------------------------------------------------
    # 实测（2026-10-14, 914B「触摸+笔」接口）固件的一笔事件顺序是:
    #     按下: Y, X, InRange=1, TipSwitch=1
    #     抬手: Y=0, X=0, InRange=0, TipSwitch=0
    # 也就是【坐标先到、按下标志最后到】，抬手时还会先发一对 (0,0) 归零。
    # 所以: 坐标先缓冲，Y/X 成对到齐才落位；按下时把缓冲落到光标上再按；
    # 抬手时丢掉缓冲（否则光标会被 (0,0) 甩到左上角）。

    def _touch_stage(self, axis, nrm, b):
        now = time.time()
        if axis == 0x30:
            self._tsx, self._tx_t = nrm, now
        else:
            self._tsy, self._ty_t = nrm, now
        if self._tsx is None or self._tsy is None:
            return
        # ★同一份 report 里 X/Y 是连着回调的(微秒级); 隔了半拍才到的另一轴是**上一次 report
        #   的残值**。抬手时固件先发 Y=0, 若让它跟这种残值配对, 就会把 (残值X, 0) 落成一次
        #   真移动 —— 手指一离开屏幕光标就"啪"地跳到最顶部, 紧接着 release() 补发的那段拖拽
        #   也停在顶部 (2026-10-15 实测)。
        #   所以: 新到的值是 0 而搭档是残值时, 丢掉残值、把 0 留在缓冲里等下一份 report 再判:
        #     · 下一份是抬手标志 -> _touch_clear 把它丢掉（跳变消失）
        #     · 下一份是坐标（手指真滑到屏幕边上）-> 补落位, 只晚一份 report, 看不出来
        if nrm == 0.0 and abs(self._tx_t - self._ty_t) > TOUCH_PAIR_STALE_S:
            if axis == 0x30:
                self._tsy = None
            else:
                self._tsx = None
            return
        self._touch_flush(b)

    def _touch_flush(self, b):
        """把缓冲的触摸坐标落到光标上（只到齐一轴时, 另一轴沿用当前位置）。"""
        if self._tsx is None and self._tsy is None:
            return
        x = b.x if self._tsx is None else self._tsx
        y = b.y if self._tsy is None else self._tsy
        self._tsx = self._tsy = None
        if x == 0.0 and y == 0.0:
            return                        # 抬手归零的伪坐标
        b.x, b.y = x, y
        # ★必须显式 use_pen=True: post() 在「跟随光标」模式下 (takeover=False) 会把事件
        #   发到 **当前光标位置** —— 那是"移动到原地", 光标永远不动（2026-10-14 实测:
        #   手指点击通了但光标不跟手, 就是这里）。触摸和笔一样有绝对坐标, 直接用它。
        b.post(H.MOVED, use_pen=True)
        if self.debug:
            b.dbg("触摸 -> 屏幕 (%.0f, %.0f)  [基准屏 %s  %.0fx%.0f @ %.0f,%.0f]"
                  % (b.ox + x * b.w, b.oy + y * b.h, b.rect_label or "?",
                     b.w, b.h, b.ox, b.oy), min_dt=0.25)
        if b.down:
            b.on_move()                   # 按下之后交给手势判定（轻点/滑动/拖拽）

    def _touch_clear(self):
        self._tsx = self._tsy = None

    def _route_finger(self, b, cookie, kind, value):
        """双指路由: 第 2 根及以后、或双指进行中 -> 这里处理并返回 True。"""
        hit = self._cookie2finger.get(cookie) if self._cookie2finger else None
        if not hit:
            return False
        i, k = hit
        st = self._fstate[i]
        # 自愈: 新的单指按下且没有其他手指按着 -> 强制退出双指态
        # (否则 _multi 卡住时 _sync() 一直提前返回, 点击会整体失效)
        if self._multi and kind == "tip" and value and not any(
                s["down"] for j, s in enumerate(self._fstate) if j != i):
            self._multi = False
            self._two_prev = None
        if i == 0 and not self._multi:
            # 单指: 仍同步状态 —— 否则下一次双指时 active 只有第 2 根手指,
            # 永远凑不满两个, 双指起不来 (2026-10-29 实测: 只有第一轮生效)
            if kind == "tip":
                st["down"] = bool(value)
            elif kind == "inr":
                if not value:
                    st["down"] = False
            else:
                st[kind] = float(value)
            if time.time() < self._latch_until:
                return True           # 双指刚结束: 吞掉余下的单指事件
            return False              # 单指: 保持原有逻辑
        # 第 2 根手指先到: 用单指路径已经落到光标上的坐标回填第 1 指
        if i > 0 and b.down and self._fstate[0]["x"] is None:
            self._fstate[0]["x"] = b.x
            self._fstate[0]["y"] = b.y
            self._fstate[0]["down"] = True
        if kind == "tip":
            st["down"] = bool(value)
            if not value and self._multi:
                self._two_prev = None   # 手指数变了, 下次重新取基准
        elif kind == "inr":
            if not value:
                st["down"] = False
        else:
            if float(value) == 0.0:
                return True           # 抬手前固件先发 0, 不是真坐标
            st[k] = float(value)
        active = [j for j, s in enumerate(self._fstate)
                  if s["down"] and s["x"] is not None and s["y"] is not None]
        if len(active) >= 2:
            if not self._multi:
                self._multi = True
                b.down = False
                b.dragging = False
                self._want_down = False   # 否则下一次 _sync() 会把单指按下重新激活
                self._touch_clear()
                self._two_prev = None
                self._two_pend = [0.0, 0.0]
                self._two_t = 0.0
                self._pinch_prev = None
                self._pinch_dist_s = None
                self._pinch_pend = 0.0
                self._pinch_active = False
                self._pinch_acc = 0.0
                self._pinch_native_ok = True
                self._pinch_fb = 0.0
                self._pinch_n = 0
                if self._fstate[0]["x"] is None:
                    self._fstate[0]["x"] = b.x
                    self._fstate[0]["y"] = b.y
                    self._fstate[0]["down"] = True
                self.log("双指手势开始 (触点 %d)" % len(active))
            self._two_apply(b, active)
        elif not active and self._multi:
            self._multi = False
            self._two_prev = None
            self._pinch_prev = None
            self._pinch_dist_s = None
            self._pinch_pend = 0.0
            self._latch_until = time.time() + 0.12
            if self._pinch_active:
                self._pinch_active = False
                if self._pinch_native_ok:
                    x, y = self._pinch_last_pt
                    b.pinch_native(0.0, 4, x, y)
                self._pinch_acc = 0.0
                self._pinch_fb = 0.0
            self.log("双指手势结束")
        return True

    def _two_apply(self, b, active):
        """两指: 质心 -> 滚动; 距离 -> 原生 magnify（捏合期间不再滚动）。"""
        cx = sum(self._fstate[j]["x"] for j in active) / len(active)
        cy = sum(self._fstate[j]["y"] for j in active) / len(active)
        now = time.time()

        # --- 距离 -> 缩放（先算，决定这一帧是捏合还是滚动）---
        if len(active) >= 2:
            i0, i1 = active[0], active[1]
            dxp = (self._fstate[i1]["x"] - self._fstate[i0]["x"]) * b.w
            dyp = (self._fstate[i1]["y"] - self._fstate[i0]["y"]) * b.h
            dist = (dxp * dxp + dyp * dyp) ** 0.5
            self._pinch_dist_s = dist
            if self._pinch_prev is None or self._pinch_prev <= 1.0:
                self._pinch_prev = self._pinch_dist_s
            else:
                delta = (self._pinch_dist_s / self._pinch_prev) - 1.0
                self._pinch_prev = self._pinch_dist_s
                if not self._pinch_active:
                    self._pinch_acc += delta
                    if abs(self._pinch_acc) >= 0.015:
                        self._pinch_active = True
                        # 锚点固定在捏合开始的质心 —— 期间不再移动, 否则画面会跟着漂
                        self._pinch_anchor = (b.ox + cx * b.w, b.oy + cy * b.h)
                        ax, ay = self._pinch_anchor
                        self.log("捏合开始 (锚点 %.0f,%.0f)" % (ax, ay))
                        if not b.pinch_native(0.0, 1, ax, ay):
                            self._pinch_native_ok = False
                            self.log("原生 magnify 创建失败 -> 回退档位")
                if self._pinch_active and now - self._pinch_t >= 0.008:
                    self._pinch_t = now
                    ax, ay = self._pinch_anchor
                    if self._pinch_native_ok:
                        if not b.pinch_native(delta * 3.0, 2, ax, ay):
                            self._pinch_native_ok = False
                        else:
                            self._pinch_n += 1
                            if self._pinch_n % 10 == 1:
                                self.log("捏合 changed %d 次 (本次 delta %+.3f)"
                                         % (self._pinch_n, delta))
                    if not self._pinch_native_ok:
                        self._pinch_fb += delta
                        if abs(self._pinch_fb) >= 0.10:
                            b.zoom_step(1 if self._pinch_fb > 0 else -1)
                            self._pinch_fb = 0.0

        # --- 质心 -> 滚动（捏合期间不发, 否则边滚边缩会抖）---
        if self._pinch_active:
            self._two_prev = (cx, cy)
            self._two_pend = [0.0, 0.0]
            return
        if self._two_prev is None:
            self._two_prev = (cx, cy)
            return
        dx = (cx - self._two_prev[0]) * b.w
        dy = (cy - self._two_prev[1]) * b.h
        self._two_prev = (cx, cy)
        self._two_pend[0] += dx
        self._two_pend[1] += dy
        if now - self._two_t < 0.011:
            return
        self._two_t = now
        px, py = self._two_pend
        if abs(px) < 0.5 and abs(py) < 0.5:
            return
        self._two_pend = [0.0, 0.0]
        b.scroll_px(px, py)

    def _feed_locked(self, page, usage, v, lo, hi, rid=PEN_REPORT_ID, cookie=0):
        if self._live_on:
            try:
                self._live_feed(page, usage, v, lo, hi, rid, cookie)
            except Exception as e:
                # 可视化只是旁路：它出错绝不能吞掉这一帧的真实输入处理
                self._log_err_once("触控可视化", e)
        # A report is positive interface evidence too. Resolve BEFORE even its
        # first coordinate, never wait for the UI timer or synthesize then switch.
        if rid == TOUCH_REPORT_ID and not self.touch_iface:
            self.touch_iface = True
            self._update_locked({})
        b = self.b
        # ★原生触控模式: 触摸接口 (report ID 1) 的一切手指事件 —— 点击/滚动/捏合 ——
        #   都由 macOS 系统直接处理, 桥接只计数、绝不合成, 否则与系统双发。
        #   笔接口 (report ID 3) 不受影响, 照常走下面的完整路径。
        if rid == TOUCH_REPORT_ID and self._mode["native_touch"]:
            self.seen["ev"] += 1
            return
        # 这串事件是不是触摸屏那一路 —— post()/press() 靠它决定坐标从哪来（见 hid_bridge.post）
        b.src_touch = (rid == TOUCH_REPORT_ID)
        if page == 0x0D and usage == 0x42:                 # TipSwitch
            if rid == TOUCH_REPORT_ID:
                if self._route_finger(b, cookie, "tip", v):
                    return
                if self._touch_cookies and cookie and cookie not in self._touch_cookies:
                    return                                      # 不是第 1 根手指 -> 忽略
                self.seen["ev"] += 1
                self.seen["tip"] += 1
                if v:
                    self._touch_flush(b)   # 坐标已在缓冲里 -> 先把光标带过去再按
                    self._want_down = True
                    if self.debug:
                        b.dbg("触摸: 按下 (定点 %.0f,%.0f)"
                              % (b.ox + b.x * b.w, b.oy + b.y * b.h))
                else:
                    self._touch_clear()    # 抬手: 丢掉缓冲(固件刚发的那对 0,0 不算数)
                    self._want_down = False
                    if self.debug:
                        b.dbg("触摸: 抬起")
                self._sync()
                self.total["fwd"] = b.n
                return
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
            if rid == TOUCH_REPORT_ID:
                # 触摸侧松开以 TipSwitch 为准；InRange 只用来把缓冲里的坐标落位/清掉
                if self._route_finger(b, cookie, "inr", v):
                    return
                if self._touch_cookies and cookie and cookie not in self._touch_cookies:
                    return
                if v:
                    self._touch_flush(b)
                else:
                    self._touch_clear()
                return
            if not v:
                self._want_down = False
                self.pres_cur = 0
                self._sync()
        elif page == 0x01 and usage in (0x30, 0x31) and hi > lo and lo >= 0:
            # 绝对坐标 (笔/触摸接口); 相对坐标的鼠标接口 lo < 0, 会被排除
            nrm0 = float(v - lo) / (hi - lo)
            if rid == TOUCH_REPORT_ID and self._route_finger(
                    b, cookie, "x" if usage == 0x30 else "y", nrm0):
                return
            if rid == TOUCH_REPORT_ID and self._touch_cookies and cookie \
                    and cookie not in self._touch_cookies:
                return                                      # 不是第 1 根手指 -> 忽略
            self.seen["ev"] += 1
            self.seen["x" if usage == 0x30 else "y"] += 1
            nrm = nrm0
            if rid == TOUCH_REPORT_ID:
                self._touch_stage(usage, nrm, b)   # 触摸: 先缓冲, 成对到齐才落位
            else:
                if usage == 0x30:
                    b.x = nrm
                else:
                    b.y = nrm
                if b.takeover and not b.down:
                    # 接管了绝对坐标: 笔就是鼠标, 悬停也得把光标挪过去
                    b.post(H.MOVED)
                elif b.down:
                    # ★按下之后的移动一律交给手势判定 —— 快速滑动(滚动/划选), 停住再滑(拖拽),
                    #   全在 on_move 里按绑定表决定, 判定之前一个鼠标按键都不发。
                    #   这里曾经读过一个不存在的 b.scroll 属性: 笔一按下去就抛 AttributeError,
                    #   回调被 except 吞掉 -> 拖拽和滚动全部失效 (2026-09-18 定位)。
                    b.on_move()
                if self.debug:
                    # 每 0.4 秒一条: 笔尖位置 vs 系统光标。光标没跟着笔走时一眼看出来。
                    b.dbg(self._dbg_hover_line(b), min_dt=0.4)
        self.total["fwd"] = b.n
        self.total["scroll"] = b.ns
        self.total["drag"] = b.nd
        self.total["click"] = b.nclk

    def _sync(self):
        """TipSwitch / 压力 / InRange 变化后统一处理按下与抬起"""
        if self._multi:
            return                    # 双指手势期间不产生单指按下/抬起
        b = self.b
        want = getattr(self, "_want_down", False)
        if want == b.down:
            return
        if want:
            b.press()
        else:
            if b.dragging:
                b.last_drag = 0.0     # 抬起前补发最后一段拖拽
                b.drag()
            b.release()

    # ---------------- 触控可视化 (只读) ----------------

    def set_live(self, on):
        """打开/关闭只读触点快照。面板关掉就停，不占 HID 热路径。"""
        with self._live_lk:
            self._live_on = bool(on)
            if not on:
                self._live_fingers = []
                self._live_pen = {"x": None, "y": None, "px": None, "py": None,
                                  "down": False, "inr": False, "pres": 0, "t": 0.0}
                self._live_jumps = 0

    def live_snapshot(self):
        with self._live_lk:
            return {
                "now": time.time(),
                "fingers": [dict(f) for f in self._live_fingers],
                "pen": dict(self._live_pen),
                "touch_iface": bool(self.touch_iface),
                "native": bool(self._mode.get("native_touch")),
                "running": bool(self.running),
                "rect": ((self.b.ox, self.b.oy, self.b.w, self.b.h)
                         if self.b is not None else None),
            }

    @staticmethod
    def _live_axis(st, key, val):
        """单轴采样。归零/顶满的突然跳变是抬手标记，不采信。

        实测：抬手先发 Y=0、X=0（笔是 X=0、Y=max），之后才来 InRange/TipSwitch=0。
        只看单轴就能认出来 —— 从非边缘值突然跳到 0 或 1，就是复位标记，不是真实位置。
        """
        prev = st.get(key)
        if prev is not None:
            if val <= 0.002 and prev > 0.05:
                return False
            if val >= 0.998 and prev < 0.95:
                return False
        st[key] = val
        return True

    def _live_commit(self, st, nx, ny, now, label=""):
        ox, oy = st["x"], st["y"]
        if (ox is not None and oy is not None
                and (abs(nx - ox) > 0.35 or abs(ny - oy) > 0.35) and self._live_jumps < 20):
            self._live_jumps += 1
            self.log("LIVE jump %s: (%.2f,%.2f) -> (%.2f,%.2f)"
                     % (label or "?", ox, oy, nx, ny))
        st["x"], st["y"], st["t"] = nx, ny, now

    def _live_feed(self, page, usage, v, lo, hi, rid, cookie):
        """把原始报告抄一份归一化坐标出来。只读：不改桥接状态、不合成事件。"""
        now = time.time()
        with self._live_lk:
            if rid == TOUCH_REPORT_ID:
                if len(self._live_fingers) != len(self._finger_map):
                    self._live_fingers = [{"x": None, "y": None, "down": False, "t": 0.0}
                                          for _ in self._finger_map]
                for i, m in enumerate(self._finger_map):
                    st = self._live_fingers[i]
                    label = "f%d" % i
                    if cookie == m.get("x") and hi > lo:
                        if self._live_axis(st, "x", (v - lo) / float(hi - lo)) \
                                and st["y"] is not None and st["down"]:
                            self._live_commit(st, st["x"], st["y"], now, label)
                    elif cookie == m.get("y") and hi > lo:
                        if self._live_axis(st, "y", (v - lo) / float(hi - lo)) \
                                and st["x"] is not None and st["down"]:
                            self._live_commit(st, st["x"], st["y"], now, label)
                    elif cookie == m.get("tip"):
                        st["down"] = bool(v)
                        st["t"] = now
                        if v and st["x"] is not None and st["y"] is not None:
                            self._live_commit(st, st["x"], st["y"], now, label)
                    else:
                        continue
                    return
            elif rid == PEN_REPORT_ID:
                p = self._live_pen
                if page == 0x01 and usage == 0x30 and hi > lo:
                    if self._live_axis(p, "x", (v - lo) / float(hi - lo)) and p["y"] is not None:
                        self._live_commit(p, p["x"], p["y"], now, "pen")
                elif page == 0x01 and usage == 0x31 and hi > lo:
                    if self._live_axis(p, "y", (v - lo) / float(hi - lo)) and p["x"] is not None:
                        self._live_commit(p, p["x"], p["y"], now, "pen")
                elif page == 0x0D and usage == 0x32:      # InRange（这个笔接口没有，留兼容）
                    p["inr"] = bool(v)
                    p["t"] = now
                elif page == 0x0D and usage == 0x42:      # TipSwitch
                    p["down"] = bool(v)
                    p["t"] = now
                    if v and p["x"] is not None and p["y"] is not None:
                        self._live_commit(p, p["x"], p["y"], now, "pen")
                elif page == 0x0D and usage == 0x30:      # TipPressure
                    p["pres"] = v
                    p["t"] = now

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
            "touch_iface": self.touch_iface,
            "fwd": self.b.n if self.b else 0,
            "scroll": self.b.ns if self.b else 0,
            "click": self.b.nclk if self.b else 0,
            "drag": self.b.nd if self.b else 0,
            "rclick": self.b.nrc if self.b else 0,
            "down": bool(self.b.down) if self.b else False,
            # 手势 -> 动作: 状态行按它决定报「滚动」还是「拖拽」
            "binds": H.binds_from_cfg(self.cfg),
            "target": self.target.get("label", ""),     # 光标基准屏
            "target_takeover": bool(self.target.get("takeover")),
            **self._mode,
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
        tail = counters_tail(st)
        return "已连接 · %s · %s" % (names, tail)

    def status_line_short(self):
        """菜单栏第一行。不带括号、也不解释原因 —— 原因在设置窗口的权限那一段里说。"""
        st = self.status()
        if not st["running"]:
            return "已暂停" if st["rc"] >= 0 else "未运行 · 缺少输入监控权限"
        if not st["devices"]:
            return "已开启 · 等平板上线"
        names = "、".join(d[0] for d in st["devices"])
        tail = counters_tail(st)
        return "已连接 · %s · %s" % (names, tail)
