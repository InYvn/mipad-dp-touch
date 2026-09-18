#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小米平板 9 Pro Max (DP-in 便携屏) HID 诊断 / 触控桥接

设备事实 (ioreg + 实跑实测):
  USB 复合设备 PID=0x2D05(11525), VID 会变: 0x2717(10007, 小米) / 0x18D1(6353, Google 通用身份)
    -> 认设备一律按 ProductID, 见 make_match()
  接口 0/1 : MTP
  接口 2   : HID  DeviceUsagePage=1  Keyboard / Mouse(相对坐标)   <- 平时不出数据
  接口 3   : HID  DeviceUsagePage=13 Pen(Digitizer)               <- 笔/触控回传
             其中: 坐标 X/Y 的 usage 是 GenericDesktop(0x01/0x30,0x31) 的【绝对坐标】
                   压力/按键走 Digitizer: TipPressure 0x30, InRange 0x32, XTilt 0x3D,
                   YTilt 0x3E, TipSwitch 0x42, BarrelSwitch 0x44, Eraser 0x45
  实测: 笔移动时 X/Y 大量上报; TipSwitch 有上报, 但 macOS 不把它翻成鼠标左键

用法:
  # 在仓库根目录执行 (源码在 src/)
  python3 src/hid_bridge.py scan             # 列出匹配到的 HID 设备
  python3 src/hid_bridge.py listen 30        # 监听并汇总(按接口分类)
  python3 src/hid_bridge.py listen 30 raw    # 额外打印逐帧数值
  python3 src/hid_bridge.py probe 12         # 分三轮: 笔悬停 / 笔按下 / 只用手指  (关键!)
  python3 src/hid_bridge.py finger 12        # 分两轮: 笔放远只用手指 / 只用笔悬停 (指纹对比, 判定手指到底有没有上报)
  python3 src/hid_bridge.py bridge           # ★触屏式 (默认): 轻点=左键单击, 快速划=滚动 (不选中文字),
                                             #   笔尖停住 0.25s 再划=拖拽/划选, 停住不动 0.8s=右键菜单
  python3 src/hid_bridge.py bridge --drag-pen    # 拖拽位置改用笔绝对坐标 (默认用系统光标位置)
  python3 src/hid_bridge.py bridge --bind-swipe=drag --bind-hold=none
                                             # 改按键绑定: 手势 tap/swipe/hold_swipe/hold
                                             #   动作 none/left/right/middle/double/scroll/drag/space/back
                                             #   老开关仍在: --select (划动=拖拽) --no-hold-drag --no-rc
                                             #   判定时长: --hold-drag-ms=N --rc-hold-ms=N; 方向反了: --scroll-flip
  python3 src/hid_bridge.py bridge --takeover    # 全接管: X/Y 也由脚本驱动 (默认只跟随系统光标)

权限: listen/probe/scan 需『输入监控』; bridge 另需『辅助功能』
"""
import ctypes
import ctypes.util
import datetime
import sys
import time

VENDOR = 0x2717        # 小米身份; 匹配走白名单, 这里只留给诊断输出
PRODUCT = 0x2D05
TABLET_VIDS = (0x2717, 0x18D1)   # 「主机看平板」的两个身份: 小米 / Google 通用
# 下面两个跟设备匹配无关, 别混用:
#   0x05AC = Apple, 是「平板看主机」方向的 DP Alt Mode SVID —— 平板的
#   usb_dp_relay 读到 adapter_svid: 1452(=0x05AC) 就会判 is_mac=1, 只建
#   Pen-only(107B) 描述符, 于是 macOS 上永远没有手指触控。
#   它不会、也不能出现在「主机看平板」的设备 VID 里。
APPLE_SVID = 0x05AC
PEN_USAGE_PAGE = 0x0D            # Digitizer: 笔集合所在接口
PEN_USAGE = 0x02                 # Pen
RAW_LIMIT = 300

cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))

STR = 0x08000100
SINT32 = 3
DEFAULT_MODE = b"kCFRunLoopDefaultMode"

cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
cf.CFNumberCreate.restype = ctypes.c_void_p
cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
cf.CFNumberGetValue.restype = ctypes.c_bool
cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
cf.CFDictionaryCreate.restype = ctypes.c_void_p
cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                  ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                  ctypes.c_void_p, ctypes.c_void_p]
cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
cf.CFRunLoopRunInMode.restype = ctypes.c_int
cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
cf.CFRunLoopStop.restype = None
cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
cf.CFSetGetCount.restype = ctypes.c_long
cf.CFSetGetCount.argtypes = [ctypes.c_void_p]

# CFDictionaryCreate 的 callbacks 绝不能传 NULL —— 传 NULL 得到的字典会让 AX 内部
# 在查找时段错误(实测); 必须用真正的 kCFTypeDictionary*CallBacks
CF_KB = lambda: ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
CF_VB = lambda: ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
CF_AB = lambda: ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
cf.CFArrayCreate.restype = ctypes.c_void_p
cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
cf.CFRelease.restype = None
cf.CFRelease.argtypes = [ctypes.c_void_p]
cf.CFSetGetCount.restype = ctypes.c_long
cf.CFSetGetCount.argtypes = [ctypes.c_void_p]

iokit.IOHIDManagerCreate.restype = ctypes.c_void_p
iokit.IOHIDManagerCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
iokit.IOHIDManagerSetDeviceMatching.restype = None
iokit.IOHIDManagerSetDeviceMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
iokit.IOHIDManagerSetDeviceMatchingMultiple.restype = None
iokit.IOHIDManagerSetDeviceMatchingMultiple.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
iokit.IOHIDManagerCopyDevices.restype = ctypes.c_void_p
iokit.IOHIDManagerCopyDevices.argtypes = [ctypes.c_void_p]
iokit.IOHIDManagerOpen.restype = ctypes.c_int
iokit.IOHIDManagerOpen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
iokit.IOHIDManagerScheduleWithRunLoop.restype = None
iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
iokit.IOHIDDeviceGetProperty.restype = ctypes.c_void_p
iokit.IOHIDDeviceGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

# 指针参数必须声明 argtypes, 否则 64 位被截成 int32 -> 段错误
for _fn, _rt in (("IOHIDValueGetElement", ctypes.c_void_p),
                 ("IOHIDElementGetUsagePage", ctypes.c_uint32),
                 ("IOHIDElementGetUsage", ctypes.c_uint32),
                 ("IOHIDElementGetLogicalMin", ctypes.c_long),
                 ("IOHIDElementGetLogicalMax", ctypes.c_long),
                 ("IOHIDValueGetIntegerValue", ctypes.c_long),
                 ("IOHIDValueGetTimeStamp", ctypes.c_uint64)):
    _f = getattr(iokit, _fn)
    _f.restype = _rt
    _f.argtypes = [ctypes.c_void_p]

PAGE_NAMES = {0x01: "GenericDesktop", 0x07: "Keyboard", 0x08: "LED", 0x0D: "Digitizer"}
GD = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x38: "Wheel", 0x01: "Pointer", 0x02: "Mouse", 0x06: "Keyboard"}
DIG = {0x20: "Stylus", 0x21: "Puck", 0x22: "Finger", 0x23: "DeviceSettings", 0x30: "TipPressure",
       0x32: "InRange", 0x3D: "XTilt", 0x3E: "YTilt", 0x42: "TipSwitch", 0x44: "BarrelSwitch",
       0x45: "Eraser", 0x47: "Confidence", 0x48: "Width", 0x49: "Height", 0x51: "ContactID"}


def uname(page, usage):
    if page == 0x01:
        return GD.get(usage, "GD_0x%02X" % usage)
    if page == 0x0D:
        return DIG.get(usage, "DIG_0x%02X" % usage)
    return "0x%02X/0x%02X" % (page, usage)


def cfstr(s):
    return cf.CFStringCreateWithCString(None, s, STR)


def cfnum(v):
    i = ctypes.c_int32(v)
    return cf.CFNumberCreate(None, SINT32, ctypes.byref(i))


def make_match():
    """匹配表: VID 白名单 × ProductID × 笔接口 usage, 三条件同时成立。

    三个条件缺一不可：
      · 写死 0x2717 -> 平板开 USB 调试后身份变 0x18D1, 命中 0 个设备
        （平板侧 HID 完好、macOS 也挂上了笔, 但 App 一个都打不开,
        表现就是「等待平板上线」+ 笔全失效）;
      · 只按 0x18D1 -> 那是通用 Android 身份, 机器上别的 Android 设备也会被收进来;
      · 加上 PID + 笔接口 usage -> 普通 Android 设备进不来。
    实测(2026-09-17 平板在 DP-in): 严格表 -> 1 个接口(笔集合);
    VID 白名单 -> 2 个接口; 写死 0x2717 + PID -> 0 个接口。
    """
    dicts = []
    for vid in TABLET_VIDS:
        items = [(b"VendorID", vid), (b"ProductID", PRODUCT),
                 (b"PrimaryUsagePage", PEN_USAGE_PAGE), (b"PrimaryUsage", PEN_USAGE)]
        n = len(items)
        keys = (ctypes.c_void_p * n)(*[cfstr(k) for k, _v in items])
        vals = (ctypes.c_void_p * n)(*[cfnum(v) for _k, v in items])
        dicts.append(cf.CFDictionaryCreate(None, keys, vals, n, CF_KB(), CF_VB()))
    a = (ctypes.c_void_p * len(dicts))(*dicts)
    arr = cf.CFArrayCreate(None, a, len(dicts), CF_AB())
    for d in dicts:
        cf.CFRelease(d)
    return arr


def ax_opts(prompt=True):
    """AXIsProcessTrustedWithOptions 的选项字典。

    !!! 不要把这个字典传给 AXIsProcessTrustedWithOptions —— 见
    ax_request_permission() 的说明, 那条 ctypes 路径在 macOS 27 上必然段错误。
    """
    KTRUE = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
    keys = (ctypes.c_void_p * 1)(cfstr(b"AXTrustedCheckOptionPrompt"))
    vals = (ctypes.c_void_p * 1)(KTRUE if prompt else 0)
    return cf.CFDictionaryCreate(None, keys, vals, 1, CF_KB(), CF_VB())


def ax_request_permission(prompt=True):
    """触发系统『辅助功能』授权弹窗, 返回当前是否已授权。

    ------------------------------------------------------------------
    实测 (macOS 27.0 / 26A428, 2026-09-16), 崩溃点是 AX 内部第一个 CFGetTypeID:

        传 NULL               -> 正常返回 False
        ctypes 手工造的字典   -> SIGSEGV  (声明/不声明 argtypes 都一样崩)
        PyObjC 桥接的字典     -> 正常返回 False      <-- 本函数走这条

    那个 ctypes 字典本身是合法的: CFShow 打得出来、CFGetTypeID = 18
    (CFDictionary)、CFDictionaryGetCount = 1 全正常 —— 所以问题在 ctypes
    这条调用路径, 不在字典内容。结论: 一律用 PyObjC; PyObjC 不可用时返回
    False, 由调用方打开系统设置面板, 绝不冒段错误的风险。

    注意 SIGSEGV 是信号不是 Python 异常, 调用方的 try/except 拦不住 ——
    这正是本函数存在的意义。
    ------------------------------------------------------------------
    """
    try:
        import ApplicationServices as AS
        return bool(AS.AXIsProcessTrustedWithOptions(
            {AS.kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        return False


DEVU = {}


def dev_usage_page(dev):
    """回调的 sender 就是 IOHIDDeviceRef; 用它区分『键鼠接口』与『笔接口』"""
    k = int(dev) if dev else 0
    if k in DEVU:
        return DEVU[k]
    v = -1
    prop = iokit.IOHIDDeviceGetProperty(dev, cfstr(b"DeviceUsagePage"))
    if prop:
        out = ctypes.c_int32(0)
        if cf.CFNumberGetValue(prop, SINT32, ctypes.byref(out)):
            v = out.value
    DEVU[k] = v
    return v


CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
iokit.IOHIDManagerRegisterInputValueCallback.restype = None
iokit.IOHIDManagerRegisterInputValueCallback.argtypes = [ctypes.c_void_p, CALLBACK, ctypes.c_void_p]

KEEP = []  # 保住回调对象引用
stats = {}
lastv = {}
rng = {}
RAW = {"on": False, "n": 0}


def run(cb, seconds):
    mgr = iokit.IOHIDManagerCreate(None, 0)
    iokit.IOHIDManagerSetDeviceMatchingMultiple(mgr, make_match())
    if cb is not None:
        KEEP.append(cb)
        iokit.IOHIDManagerRegisterInputValueCallback(mgr, cb, None)
    iokit.IOHIDManagerScheduleWithRunLoop(mgr, cf.CFRunLoopGetCurrent(), cfstr(DEFAULT_MODE))
    rc = iokit.IOHIDManagerOpen(mgr, 0)
    devs = iokit.IOHIDManagerCopyDevices(mgr)
    if rc != 0:
        print("IOHIDManagerOpen rc = %d  <-- 缺『输入监控』权限" % rc)
        print("   系统设置 > 隐私与安全性 > 输入监控 -> 打开 Terminal, 完全退出后重开再跑")
        return rc
    print("设备已打开 (匹配 %d 个 HID 接口)" % (cf.CFSetGetCount(devs) if devs else 0))
    mode = cfstr(DEFAULT_MODE)
    end = time.time() + seconds
    while time.time() < end:
        cf.CFRunLoopRunInMode(mode, 0.1, False)
    return rc


def dump_stats(title="汇总"):
    print("\n----- %s -----" % title)
    if not stats:
        print("  (无任何事件)")
        return
    pen_xy = sum(c for (dp, p, u), c in stats.items() if dp == 13 and p == 0x01 and u in (0x30, 0x31))
    kb_xy = sum(c for (dp, p, u), c in stats.items() if dp == 1 and p == 0x01 and u in (0x30, 0x31))
    for (dp, p, u), c in sorted(stats.items(), key=lambda x: -x[1]):
        src = {13: "笔接口", 1: "键鼠接口"}.get(dp, "dev%d" % dp)
        rr = ("  范围 %d..%d" % rng[(dp, p, u)]) if (dp, p, u) in rng else ""
        print("  %-9s %-14s %-13s %6d 次  最后=%s%s" % (src, PAGE_NAMES.get(p, hex(p)), uname(p, u), c, lastv.get((dp, p, u)), rr))
    print("  § 笔接口坐标事件 %d 次 / 键鼠接口坐标事件 %d 次" % (pen_xy, kb_xy))
    finger = [k for k in stats if k[1] == 0x0D and k[2] in (0x22, 0x51)]
    print("  § 多指/接触标识(Finger,ContactID) 事件: %s" % ("有 %s" % finger if finger else "无"))


def make_cb():
    @CALLBACK
    def cb(ctx, result, sender, value):
        try:
            if not value:
                return
            el = iokit.IOHIDValueGetElement(value)
            if not el:
                return
            dp = dev_usage_page(sender)
            page = iokit.IOHIDElementGetUsagePage(el)
            usage = iokit.IOHIDElementGetUsage(el)
            v = iokit.IOHIDValueGetIntegerValue(value)
            lo = iokit.IOHIDElementGetLogicalMin(el)
            hi = iokit.IOHIDElementGetLogicalMax(el)
            k = (dp, page, usage)
            stats[k] = stats.get(k, 0) + 1
            lastv[k] = v
            rng[k] = (lo, hi)
            if RAW["on"] and RAW["n"] < RAW_LIMIT:
                RAW["n"] += 1
                print("    [%s] %-13s = %-8s (%d..%d)" % (dp, uname(page, usage), v, lo, hi))
        except Exception as e:
            print("    [cb err] %r" % (e,))
    return cb


def cmd_listen(seconds, raw):
    RAW["on"] = raw
    print("===== 监听 %gs  %s =====" % (seconds, datetime.datetime.now().isoformat(timespec="seconds")))
    print("请做三件事: ①笔悬停移动 ②笔尖按下画/点 ③手指划几下")
    run(make_cb(), seconds)
    dump_stats()


def cmd_probe(sec):
    rounds = [
        "① 只用笔, 让笔**悬停**在屏幕上方滑动 (笔尖不要接触, 只移动)",
        "② 用笔尖**压住**屏幕, 画几条线 / 点几下图标",
        "③ 把笔拿走, **只用手指**在屏幕上划动、点击",
    ]
    for i, desc in enumerate(rounds, 1):
        print("\n" + "=" * 64)
        print("第 %d/3 轮 (%.0f 秒后开始):  %s" % (i, sec, desc))
        print("=" * 64)
        sys.stdout.flush()
        for n in range(int(sec), 0, -1):
            print("  %d..." % n)
            sys.stdout.flush()
            time.sleep(1)
        stats.clear(); lastv.clear(); rng.clear(); RAW["n"] = 0
        print("  >>> 开始! <<<")
        sys.stdout.flush()
        run(make_cb(), sec)
        dump_stats("第 %d 轮结果: %s" % (i, desc))
    print("\n提示: 若第 3 轮(手指)事件数极少/为 0, 说明手指触控没有被固件送出来。")


def cmd_finger(sec):
    print("""
这个测试要区分『手指』和『笔悬停』在 HID 层留下的指纹。
关键前提: 电磁笔在离屏 1~2cm 内就会被感应到并持续上报坐标 ——
所以之前 probe 第 3 轮(号称"只用手指")拿到的坐标, 很可能是笔悬停污染的。
这一轮请把笔真的放远。
""")
    rounds = [
        "① 把笔拿到 **1 米以外** (最好关机或放到另一个房间), 然后**只用手指**在屏上划动 + 点击",
        "② 笔拿回来, **只用笔悬停**在屏幕上方滑动 (笔尖不要接触屏幕)",
    ]
    snaps = []
    for i, desc in enumerate(rounds, 1):
        print("\n" + "=" * 64)
        print("第 %d/2 轮 (%.0f 秒后开始):  %s" % (i, sec, desc))
        print("=" * 64)
        sys.stdout.flush()
        for n in range(int(sec), 0, -1):
            print("  %d..." % n)
            sys.stdout.flush()
            time.sleep(1)
        stats.clear(); lastv.clear(); rng.clear(); RAW["n"] = 0
        print("  >>> 开始! <<<")
        sys.stdout.flush()
        run(make_cb(), sec)
        dump_stats("第 %d 轮结果: %s" % (i, desc))
        s = {}
        for k, n in stats.items():
            s.setdefault(k[1], {})[k[2]] = n
        snaps.append(s)

    def cnt(s, page, usages):
        return sum(s.get(page, {}).get(u, 0) for u in usages)

    xy = lambda s: cnt(s, 0x01, (0x30, 0x31))
    pres = lambda s: cnt(s, 0x0D, (0x30,))
    tip = lambda s: cnt(s, 0x0D, (0x42,))
    tilt = lambda s: cnt(s, 0x0D, (0x3D, 0x3E))

    print("\n" + "=" * 64)
    print("判读 (笔已放远 vs 笔悬停, 两轮指纹对比)")
    print("=" * 64)
    for name, s in (("轮1 手指(笔已放远)", snaps[0]), ("轮2 笔悬停", snaps[1])):
        print("  %-20s 坐标 %5d   压力 %5d   TipSwitch %4d   倾角 %4d" % (name, xy(s), pres(s), tip(s), tilt(s)))
    r1, r2 = snaps
    print()
    if xy(r1) == 0 and pres(r1) == 0:
        print("  => 轮1 零事件: **手指触控根本没有从平板送出来**。")
        print("     这不是 macOS 端能修的 —— 固件/平板侧就没往 USB 送手指数据。")
        print("     唯一出路: 用笔操作, 或换软件副屏方案(Duet Display / Deskreen)。")
    elif tilt(r1) > 0:
        print("  => 轮1 出现了倾角数据: 这些事件其实来自**笔**(笔仍在感应范围内)。")
        print("     请把笔真的放到 1 米外/关机, 重跑一次这个测试。")
    else:
        print("  => 轮1 有坐标/压力但**没有倾角**: 手指确实有独立上报, 而且")
        print("     『无倾角 + 无 TipSwitch + 压力偏低』可以作为手指的指纹,")
        print("     接下来在 bridge 里据此区分笔/手指. 请把上面两轮的完整输出发我。")


# ---------------- bridge: TipSwitch -> 鼠标左键 ----------------
class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
cg.CGEventPost.restype = None
cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
cg.CGEventCreate.restype = ctypes.c_void_p
cg.CGEventCreate.argtypes = [ctypes.c_void_p]
cg.CGEventGetLocation.restype = CGPoint
cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
cg.CGMainDisplayID.restype = ctypes.c_uint32
cg.CGDisplayBounds.restype = CGRect
cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
# 滚轮事件是 C 可变参数函数 (…, int32_t wheel1, ...): 声明了 argtypes 反而传不进可变部分,
# 所以这里【只】声明 restype —— 不声明的话 64 位指针会被截成 int32 直接崩。
cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
# 键盘事件 / 事件字段: 不声明 argtypes 的话 64 位指针会被截成 int32 (见上面那条注释)
cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
cg.CGEventSetFlags.restype = None
cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
cg.CGEventSetIntegerValueField.restype = None
cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
# 事件字段号: kCGMouseEventClickState(1) 让「双击」真的是双击 (应用看 clickCount),
# kCGMouseEventButtonNumber(3) 让中键事件带对按键号。
CLICK_STATE_FIELD = 1
BUTTON_NUM_FIELD = 3
MIDDLE_BUTTON = 2
CMD_MASK = 1 << 20      # kCGEventFlagMaskCommand

ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
ax.AXIsProcessTrusted.restype = ctypes.c_bool

MOVED, LDOWN, LUP, LDRAGGED = 5, 1, 2, 6
RDOWN, RUP = 3, 4        # kCGEventRightMouseDown / Up
SCROLL_UNIT_PIXEL = 0   # kCGScrollEventUnitPixel


DRAG_MIN_DT = 0.008     # 拖拽/滚动事件最小间隔 (~125Hz), 防止 X/Y 双流把事件量翻倍
SCROLL_TAP_PX = 12      # --scroll 触屏模式: 位移小于此像素数算『轻点』(点击), 超过则进入滚动
HOLD_DRAG_DT = 0.25     # ★长按拖拽: 笔尖先停住这么久再划 -> 当作拖拽 (修「触屏模式拖不动窗口」)
HOLD_RADIUS_PX = 10     # 「停住」的半径: 期间笔尖飘出这个圈就不算长按, 免得误判成拖拽
                        #   (原来 6px 太紧: 笔尖本身有 1~5px 抖动, 会把「停住再划」也判掉)
# ★长按不动=右键 的容差(px): 抬手时拿【中位偏移】跟它比, 峰值抖动不算
#   —— 真人握着笔「不动」时笔尖也有 1~5px 漂移, 拿峰值判就永远触发不了。
RC_MAX_PX = 30
# 这一手已经被那点抖动「误升成拖拽」时用更严的圈: 真拖拽会一直被拉远, 抖动只在原地晃。
RC_PHANTOM_MAX_PX = 14
HOLD_ARM_PX = 2         # 长按成立后笔尖动这么多就升级成拖拽 (零死区: 手感"跟手", 而不是先空滑十几像素)
HOLD_MS_OPTS = (200, 250, 350, 500)   # 长按判定档位 (ms): 窗口下拉与 CLI 共用同一份
RC_HOLD_MS_OPTS = (0, 600, 800, 1000)  # ★长按不动 = 右键菜单 的档位 (ms); 0 = 关。窗口下拉与 CLI 共用同一份
RC_HOLD_DT = 0.8       # 默认: 笔尖停住不动 0.8s -> 抬手时弹右键菜单 (长按之后又划走 => 仍是拖拽)

OTHERS_DOWN, OTHERS_UP = 25, 26   # kCGEventOtherMouseDown / Up —— 中键就靠这俩

# ---------------------------------------------------------------------------
# 按键绑定表 —— 「一笔操作」怎么归类, 每一类发什么动作
#
# 四类手势 (按形态分, 不是按功能分):
#   tap        轻点        笔尖点一下就走
#   swipe      快速滑动     一笔划过去
#   hold_swipe 停住再滑     笔尖先停住、再划
#   hold       停住不动     笔尖停住又没划走, 抬手那一刻算
#
# 默认值 = 2026-09 之前的硬编码行为, 所以升级上来的用户什么都不用改;
# 窗口里的「恢复默认」就是把 DEFAULT_BINDS 写回去。
#
# ★ 这张表是唯一出处: 窗口下拉 / CLI 的 --bind-<手势>=<动作> / Bridge 的派发
#   全都读它 —— 界面上出现过的选项一定有实现, 不会出现"选了没反应"的死标签。
BIND_ACTIONS = (
    ("none", "关"),
    ("left", "左键单击"),
    ("right", "右键菜单"),
    ("middle", "中键单击"),
    ("double", "双击"),
    ("scroll", "滚动翻页"),
    ("drag", "拖拽"),
    ("space", "空格键"),
    ("back", "返回"),
    ("screen", "切换屏幕"),      # App 级动作: 基准屏按顺序切到下一块 (见 Bridge.switch_screen)
)
BIND_TITLES = dict(BIND_ACTIONS)

# 同一个动作在不同手势下叫法不一样 —— 说的不是实现, 是**用途**:
#   快速滑动 + 拖拽 = 划选文字 (快进快出, 一笔扫过要选的那段)
#   停住再滑 + 拖拽 = 挪窗口/图标/文件 (先按住在慢慢挪, 不然重命名/关闭按钮会误触)
# 两者发出去的事件序列完全一样 (LeftMouseDown/Dragged/Up), 差别在手势本身。
# ★「选中文字」只挂在快速滑动上 —— 停住再滑不该被说成选中文字。
BIND_ACTION_TITLES = {
    "swipe": {"drag": "拖拽 · 选中文字"},
    "hold_swipe": {"drag": "拖拽 · 移动窗口"},
}


def action_title(gesture, action):
    """某个手势下的动作叫法 (没有特指就用通用名)。"""
    return str((BIND_ACTION_TITLES.get(gesture) or {}).get(action)
               or BIND_TITLES.get(action) or action)


def gesture_action_titles(gesture):
    """窗口下拉用: 这一类手势的动作, 顺序同上, 名字用这一类手势的叫法。"""
    return tuple(action_title(gesture, a) for a in gesture_actions(gesture))

# (配置键, 行标题, 这一类手势能绑的动作, 默认动作)
# 「能绑的动作」按语义收窄: 轻点没有位移, 绑不了滚动/拖拽; 滑动那两类的位移是
# 连续量, 只给「按住状态」的动作 (滚动 / 拖拽) + 一次性动作。每个选项都有实现。
BIND_GESTURES = (
    ("tap", "轻点",
     ("none", "left", "right", "middle", "double", "space", "back", "screen"), "left"),
    ("swipe", "快速滑动",
     ("none", "scroll", "drag", "right", "middle", "space", "back", "screen"), "scroll"),
    ("hold_swipe", "停住再滑",
     ("none", "drag", "right", "middle", "space", "back", "screen"), "drag"),
    ("hold", "停住不动",
     ("none", "right", "left", "middle", "double", "space", "back", "screen"), "right"),
)
BIND_PREFIX = "bind_"
DEFAULT_BINDS = {g: d for g, _t, _o, d in BIND_GESTURES}

KEY_SPACE = 49        # kVK_Space
KEY_LBRACKET = 33     # kVK_ANSI_LeftBracket —— 配 ⌘ = 返回


def bind_key(gesture):
    """配置键: 手势 -> bind_<手势>"""
    return BIND_PREFIX + gesture


def bind_cfg_keys():
    return [bind_key(g) for g, _t, _o, _d in BIND_GESTURES]


def gesture_title(gesture):
    for g, t, _o, _d in BIND_GESTURES:
        if g == gesture:
            return t
    return gesture


def gesture_actions(gesture):
    """这一类手势能绑的动作 (窗口下拉按这个顺序列)。"""
    for g, _t, o, _d in BIND_GESTURES:
        if g == gesture:
            return tuple(o)
    return ()


def legacy_binds(cfg):
    """老配置 (mode / hold_drag / rc_hold_ms) -> 等价的四类手势绑定。

    2026-09 之前只有一个全局「滑动方式」开关和两个勾选框。升级时把它翻译过来,
    用户不必重新设一遍 (也不会有人的手感在升级后悄悄变了)。
    """
    cfg = cfg or {}
    out = dict(DEFAULT_BINDS)
    if str(cfg.get("mode") or "scroll") == "select":
        # 滑动选择: 每一笔都是拖拽 -> 快速滑动=拖拽, 另外两个手势原本就没生效
        out["swipe"] = "drag"
        out["hold_swipe"] = "none"
        out["hold"] = "none"
        return out
    if not cfg.get("hold_drag", True):
        out["hold_swipe"] = "none"
    if not int(cfg.get("rc_hold_ms") or 0):
        out["hold"] = "none"
    return out


def binds_from_cfg(cfg):
    """配置 -> 绑定表。带 bind_* 键就用它; 否则按老配置翻译 (迁移)。"""
    cfg = cfg or {}
    if not any(k in cfg for k in bind_cfg_keys()):
        return legacy_binds(cfg)
    out = dict(DEFAULT_BINDS)
    for g in DEFAULT_BINDS:
        a = cfg.get(bind_key(g))
        if a in BIND_TITLES:
            out[g] = a
    return out


class Bridge(object):
    def __init__(self, takeover=False, drag_pen=False, binds=None, scroll_flip=False,
                 hold_ms=None, rc_hold_ms=0):
        # 兜底基准 (只给命令行单独跑桥接用)。App 里由 engine 按「光标基准屏」下发,
        # 因为写死主屏 = 接了第二块屏后笔的落点会跑到别的屏上 (见 set_rect)。
        b = cg.CGDisplayBounds(cg.CGMainDisplayID())
        self.ox, self.oy, self.w, self.h = b.origin.x, b.origin.y, b.size.width, b.size.height
        # 「切换屏幕」不是合成事件, 而是换掉上面这个矩形 —— 那是 App 级的事 (要读配置、
        # 要遍历显示器), 所以 Bridge 只留一个钩子: App 起来时接上 engine.next_screen。
        # 没接 (命令行单独跑桥接) -> 这个动作什么都不做, 但绝不报错、绝不乱发事件。
        self.on_switch_screen = None
        self.rect_label = ""
        self.x = self.y = 0.0
        self.down = False
        self.n = 0
        self.nd = 0
        self.ns = 0            # 滚轮事件数
        self.ns0 = 0           # 本笔划动开始时的滚轮基数
        self.nclk = 0          # 轻点(点击)次数
        self.nrc = 0           # 右键次数
        self.last_drag = 0.0
        self.takeover = takeover
        self.drag_pen = drag_pen
        self.scroll_flip = scroll_flip
        self.bind = dict(DEFAULT_BINDS)     # 手势 -> 动作 (表见文件顶部 BIND_GESTURES)
        self.hold_dt = (float(hold_ms) / 1000.0) if hold_ms else HOLD_DRAG_DT
        self.rc_hold_ms = float(rc_hold_ms or 0)
        self.rc_hold_dt = 0.0
        self.set_binds(binds)
        self.set_rc_hold_ms(self.rc_hold_ms)
        self.gain = 1.0        # 滚动增益 (GUI 可运行时改; 1.0 = 笔走多少像素滚多少)
        # 手势判定的状态
        self.scrolled = False            # 本笔是否已超过轻点阈值 (判定为快速滑动)
        self.dragging = False            # 本笔是否已进入按住拖拽
        self.consumed = False            # 本笔是否已发过一个「一次性」动作 (右键/空格/返回…)
        self.hold_ok = True              # 「停住」期间是否一直没飘出 HOLD_RADIUS_PX
        self.hold_t0 = 0.0               # 笔尖按下的时刻 (停住判定的计时起点)
        self.nd0 = 0                     # 本笔开始时的拖拽基数 (只为日志好看)
        self.anchor_x = self.anchor_y = 0.0
        self.dev_hist = []               # [(时刻, 距锚点偏移px)]: 抬手时取中位数判「有没有在动」
        self.dev_max = 0.0               # 整笔期间的最大偏移 (日志用)
        self.last_pen_y = None
        self.pend = 0.0                  # 累积未发出的滚动位移 (px)
        self.last_scroll = 0.0
        self.tap_pos = (0.0, 0.0)
        self.drag_root = (0.0, 0.0)      # 本次拖拽的参考点 (见 drag_pos)
        self.drag_pen0 = (0.0, 0.0)      # 按下瞬间的笔尖位置 (算位移用)
        self.debug = False       # 详细日志 (排查用): 打笔尖坐标 / 光标 / 落点
        self._dbg_t = 0.0
        print("按键绑定: " + "  ".join(
            "%s=%s" % (gesture_title(g), action_title(g, self.bind[g]))
            for g, _t, _o, _d in BIND_GESTURES))
        if self.takeover:
            print("模式: 全接管 (脚本发移动+点击, 可用 --flip-y 翻 Y)")
        elif self.bind.get("swipe") == "scroll":
            print("      轻点阈值 %.0f px   滚动方向: %s"
                  % (SCROLL_TAP_PX, "翻转 (--scroll-flip)" if scroll_flip
                     else "自然 (往上划=看后面的内容)"))
        if not self.takeover and self.bind.get("hold_swipe") != "none":
            print("      「停住再滑」: 笔尖先停住 %.2fs, 之后一动就进入拖拽" % self.hold_dt)
        if self.drag_pen:
            print("      拖拽位置源: 笔的绝对坐标 (--drag-pen)")

    def set_binds(self, binds):
        """换一张绑定表 (只认表里有的手势/动作, 别的忽略)。"""
        out = dict(DEFAULT_BINDS)
        for g, a in (binds or {}).items():
            if g in DEFAULT_BINDS and a in BIND_TITLES:
                out[g] = a
        self.bind = out
        self._sync_rc()

    def set_rc_hold_ms(self, ms):
        """「停住不动」的判定时长; 这个手势绑成「关」时它不起作用。"""
        self.rc_hold_ms = float(ms or 0)
        self._sync_rc()

    def _sync_rc(self):
        self.rc_hold_dt = (self.rc_hold_ms / 1000.0) \
            if self.bind.get("hold") not in (None, "none") else 0.0

    def set_rect(self, ox, oy, w, h, label=""):
        """笔的绝对坐标铺到哪块屏的矩形上 —— 由 App 的 engine 按「光标基准屏」下发。

        多屏铁律: 基准屏必须显式给。笔报的是归一化绝对坐标, 乘进哪块屏的矩形, 光标
        就只能在哪块屏里动 —— 铺死在一块屏上, 笔就永远跨不出去。
        """
        self.ox, self.oy, self.w, self.h = float(ox), float(oy), float(w), float(h)
        self.rect_label = label or self.rect_label
        said = (self.rect_label, round(self.ox), round(self.oy),
                round(self.w), round(self.h))
        if said != getattr(self, "_rect_said", None):
            self._rect_said = said
            print("坐标基准屏: %s  origin=(%.0f,%.0f)  %.0fx%.0f"
                  % (self.rect_label or "(未命名)", self.ox, self.oy, self.w, self.h))

    def pen_screen(self):
        """笔尖绝对坐标 (归一化) -> 屏幕像素。用来和系统光标对照。"""
        return (self.ox + self.x * self.w, self.oy + self.y * self.h)

    def dbg(self, msg, min_dt=0.0):
        """详细日志: 只有开了 debug 才打; min_dt>0 时按时间节流 (笔 100+ Hz 地报)。"""
        if not self.debug:
            return
        now = time.time()
        if min_dt and (now - self._dbg_t) < min_dt:
            return
        self._dbg_t = now
        self.say(msg)

    def drag_pos(self):
        """拖拽中指针该在哪 —— 笔尖的【位移】加到按下时的参考点上。

        ★为什么不能像别的合成那样直接读系统光标: Mac 上笔不驱动光标 (「跟随光标」模式),
        每次读到的都是同一个点, 「按下 + 移动 + 抬手」就退化成一记原地按压 —— 拖拽必然失效
        (窗口挪不动、文字划不选)。相对量的另一个好处: 起点和「轻点」一致 (都落在按下瞬间的
        参考点上), 不会因为笔的绝对位置而跳一下。
        参考点: 默认取按下瞬间的光标; 勾了「拖拽位置跟随笔尖」或这块屏接管了绝对坐标,
        就用笔尖自己的位置 (那时光标本来就跟着笔走)。
        """
        px = self.ox + self.x * self.w
        py = self.oy + self.y * self.h
        if self.drag_pen:
            return (px, py)
        ax, ay = self.drag_pen0
        rx, ry = self.drag_root
        return (rx + (px - ax), ry + (py - ay))

    def post(self, kind, use_pen=None, at=None):
        pen = self.takeover if use_pen is None else use_pen
        if at is not None:                 # 显式给坐标 (拖拽走笔尖位移, 见 drag_pos)
            px, py = float(at[0]), float(at[1])
        elif pen:
            px = self.ox + self.x * self.w
            py = self.oy + self.y * self.h
        else:
            p = cg.CGEventGetLocation(cg.CGEventCreate(None))  # 当前光标位置
            px, py = p.x, p.y
        cg.CGEventPost(0, cg.CGEventCreateMouseEvent(None, kind, CGPoint(px, py), 0))
        self.n += 1
        if kind == LDRAGGED:
            self.nd += 1
        if self.n <= 3 or self.n % 200 == 0:
            print("    >> 已转发鼠标事件 %d 次 (其中拖拽 %d) kind=%d @ %.0f,%.0f down=%s"
                  % (self.n, self.nd, kind, px, py, self.down))
            sys.stdout.flush()

    def right_click(self, px=None, py=None):
        """在 (px,py) 合成一次右键。坐标不传就用当前光标位置。

        macOS 上「右键」= kCGEventRightMouseDown/Up; 菜单栏/桌面/Finder 都会因此弹右键菜单。
        """
        if px is None:
            p = cg.CGEventGetLocation(cg.CGEventCreate(None))
            px, py = p.x, p.y
        for kind in (RDOWN, RUP):
            cg.CGEventPost(0, cg.CGEventCreateMouseEvent(None, kind, CGPoint(px, py), 0))
        self.n += 2
        self.nrc += 1

    def key_tap(self, code, cmd=False):
        """敲一下键盘按键 (空格 / 返回), 按下+抬起都发"""
        for down in (True, False):
            ev = cg.CGEventCreateKeyboardEvent(None, code, down)
            if cmd:
                cg.CGEventSetFlags(ev, 1 << 20)      # kCGEventFlagMaskCommand
            cg.CGEventPost(0, ev)
        self.n += 2

    def drag(self):
        """按下状态下的移动 —— 必须发 LeftMouseDragged, 否则拖拽手势全部失效"""
        now = time.time()
        if now - self.last_drag < DRAG_MIN_DT:
            return
        self.last_drag = now
        self.post(LDRAGGED, at=self.drag_pos())    # 跟笔尖走, 不能读光标 (见 drag_pos)

    def say(self, msg):
        print("    >> " + msg)
        sys.stdout.flush()

    # ---------------- 手势判定与派发 ----------------
    # 一笔怎么走, 全看「位移 + 按住时长」落在哪一类手势里 (表见文件顶部):
    #   位移 < SCROLL_TAP_PX, 抬手时也没满足「停住不动」   -> 轻点
    #   位移 >= SCROLL_TAP_PX                            -> 快速滑动
    #   先停住 >= hold_dt 且期间没飘出圈, 之后才划          -> 停住再滑
    #   按住 >= rc_hold_dt 且整笔基本没动, 抬手             -> 停住不动
    # 每一类手势发什么动作由 self.bind 决定。
    # 关键: 判定之前**一个鼠标按键都不发** —— 所以轻点不会留下拖影,
    # 快速滑动也不会被应用解释成划选。

    def fire(self, action, px, py):
        """发一个**一次性**动作 (按下+抬起), 返回是否真的发了。

        scroll / drag 是「按住状态」的动作, 走 arm_gesture(), 不走这里。
        """
        if not action or action == "none":
            return False
        if action == "right":
            self.right_click(px, py)
            return True
        if action == "middle":
            for kind in (OTHERS_DOWN, OTHERS_UP):
                ev = cg.CGEventCreateMouseEvent(None, kind, CGPoint(px, py), 0)
                cg.CGEventSetIntegerValueField(ev, BUTTON_NUM_FIELD, MIDDLE_BUTTON)
                cg.CGEventPost(0, ev)
            self.n += 2
            return True
        if action == "space":
            self.key_tap(KEY_SPACE)
            return True
        if action == "back":
            self.key_tap(KEY_LBRACKET, cmd=True)      # ⌘[ = 返回
            return True
        if action == "double":
            self.click(px, py, 2)
            return True
        if action == "left":
            self.click(px, py, 1)
            return True
        if action == "screen":
            self.switch_screen()
            return True
        return False

    def switch_screen(self):
        """「切换屏幕」: 基准屏按顺序切到下一块 (多屏时才动)。

        与别的动作最大的不同: **一个鼠标/键盘事件都不发** —— 它只换基准矩形, 笔接着
        就在新那块屏上生效了。所以绑给「停住不动」不会在屏幕上留下任何点击。
        """
        fn = self.on_switch_screen
        if fn is None:
            self.say("切换屏幕 -> 没接上引擎, 忽略 (命令行单独跑桥接时正常)")
            return False
        try:
            return bool(fn())
        except Exception as e:
            self.say("切换屏幕失败: %r" % (e,))
            return False

    def click(self, px, py, clicks=1):
        """在 (px,py) 合成点击。clicks=2 时带上 ClickState —— 不带的话应用只当两次单击。"""
        for i in range(1, clicks + 1):
            for kind in (LDOWN, LUP):
                ev = cg.CGEventCreateMouseEvent(None, kind, CGPoint(px, py), 0)
                cg.CGEventSetIntegerValueField(ev, CLICK_STATE_FIELD, i)
                cg.CGEventPost(0, ev)
        self.n += 2 * clicks
        self.nclk += clicks

    def arm_gesture(self, action, which, gesture=""):
        """一笔已经判定成某类手势了 —— 按绑定表把动作发出去。

        drag / left : 进入按住拖拽 (起点用锚点), 之后每次移动发 LeftMouseDragged
        scroll      : 进入滚动, 之后按笔尖位移发滚轮
        其它        : 立刻在锚点发一次完整动作, 本笔就此作废 (不再重复判定)
        gesture 只用来选动作的叫法 (「拖拽」在快速滑动上是划选, 在停住再滑上是挪窗口)。
        """
        if not action or action == "none":
            self.consumed = True
            self.say("%s -> 无动作 (这一项关着)" % which)
            return
        if action in ("drag", "left"):
            self.begin_drag(which)
            self.drag()
            return
        if action == "scroll":
            self.say("%s -> 滚动 (全程不发鼠标按键, 不会选中文字)" % which)
            return
        if self.drag_pen:
            ax, ay = self.pen_screen_anchor()
        else:
            ax, ay = self.tap_pos
        if self.fire(action, ax, ay):
            self.consumed = True
            self.say("%s -> %s @%.0f,%.0f"
                     % (which, action_title(gesture, action), ax, ay))

    def press(self):
        """笔尖接触屏幕 —— 只记锚点, 一个鼠标按键都不发 (判定留给 on_move / release)"""
        self.down = True
        self.scrolled = False
        self.dragging = False
        self.consumed = False
        self.hold_ok = True
        self.hold_t0 = time.time()
        self.anchor_x, self.anchor_y = self.x, self.y
        self.dev_hist = []
        self.dev_max = 0.0
        self.last_pen_y = None
        self.pend = 0.0
        p = cg.CGEventGetLocation(cg.CGEventCreate(None))
        self.tap_pos = (p.x, p.y)   # 落点取按下瞬间的光标, 免得抬手时的微小漂移把点击带偏
        # 拖拽的两个基准 (见 drag_pos): 笔尖按下时的位置 + 这次拖拽的参考点。
        # 接管了绝对坐标的屏上, 光标本来就跟着笔走 -> 参考点直接取笔尖; 否则取光标
        # (和「轻点」的落点保持一致: 笔在这块屏上干活, 起点是你把光标停住的地方)。
        self.drag_pen0 = self.pen_screen()
        self.drag_root = self.drag_pen0 if self.takeover else self.tap_pos
        if self.debug:
            qx, qy = self.pen_screen()
            self.dbg("笔尖按下: 光标@%.0f,%.0f | 笔尖->屏 %.0f,%.0f | 差 %.0f,%.0f"
                     % (p.x, p.y, qx, qy, p.x - qx, p.y - qy))

    def release(self):
        """笔尖离开屏幕 —— 抬手这一刻把「停住不动 / 轻点」也一起判掉"""
        self.down = False
        if self.dragging:
            self.dragging = False
            self.last_drag = 0.0        # 抬起前补发最后一段, 免得丢掉拖拽末尾点位
            self.drag()
            self.post(LUP, at=self.drag_pos())
            self.say("拖拽结束 (本笔共 %d 次拖拽)" % (self.nd - self.nd0))
            # ★笔尖有 1~5px 抖动时, 「停住」期间那点抖动会让本笔【提前升级成拖拽】(门槛才 2px),
            #   于是「停住不动」在抬手前就被吃掉了。所以这里补一次判定: 这一笔其实没动 ->
            #   仍按「停住不动」处理 (前面那次无位移的拖拽等同于一次点击, 无害)。
            if self.rc_hold_dt > 0 and not self.scrolled:
                med, held = self.rc_stats()
                if held >= self.rc_hold_dt and med <= RC_PHANTOM_MAX_PX:
                    self.fire_hold(" (笔尖抖动曾被当成拖拽)")
            return
        if self.consumed:
            return                      # 本笔已经在 arm_gesture() 里发过完整动作了
        if self.scrolled:
            if self.bind.get("swipe") == "scroll":
                self.flush_scroll()
                self.say("滚动结束 (本笔共 %d 次滚轮事件, 累计 %d)" % (self.ns - self.ns0, self.ns))
            return
        # ★停住不动。判据全在抬手这一刻: 按住了够久 (rc_hold_dt) + 整笔笔尖基本没动。
        #   「没动」= 中位偏移 <= RC_MAX_PX, 不是 6px 的瞬时圈 —— 真人握笔静止时笔尖有 1~5px 抖动,
        #   偶尔还有一下尖峰, 所以只看中位数、不看峰值 (峰值只进日志), 否则这手势永远触发不了。
        #   划走超过 RC_MAX_PX 就不算停住了: 那一笔在 on_move 里已经按「快速滑动」走了。
        if self.rc_hold_dt > 0:
            med, held = self.rc_stats()
            if held >= self.rc_hold_dt and med <= RC_MAX_PX:
                self.fire_hold("")
                return
            if held >= self.rc_hold_dt:
                self.say("停住不动判定: 按住 %.2fs 但笔尖在动 (中位偏移 %.0fpx > 容差 %dpx) -> 当轻点处理"
                         % (held, med, RC_MAX_PX))
        self.fire_tap()

    def fire_hold(self, extra=""):
        """抬手时的「停住不动」-> 这个手势绑的动作 (默认右键菜单)。"""
        med, held = self.rc_stats()
        a = self.bind.get("hold", "right")
        self.pend = 0.0                # 抖动累积的那点滚动量丢掉, 只发这个动作
        px, py = self.tap_pos
        self.say("停住不动 %.2fs (中位偏移 %.0fpx / 峰值 %.0fpx) -> %s @%.0f,%.0f%s"
                 % (held, med, self.dev_max, action_title("hold", a), px, py, extra))
        self.fire(a, px, py)

    def fire_tap(self):
        """抬手时没被别的判定吃掉 -> 轻点, 发这个手势绑的动作 (默认左键单击)。"""
        px, py = self.tap_pos
        a = self.bind.get("tap", "left")
        if a == "none":
            self.say("轻点 -> 无动作 (这一项关着)")
            return
        if self.debug:
            # 这条是排查「点击没反应」的关键: 落点(按下时光标) / 笔尖位置 / 抬手时光标
            # 三者一对照就知道是「光标没跟着笔走」还是「点击发出去被系统吃了」。
            qx, qy = self.pen_screen()
            c = cg.CGEventGetLocation(cg.CGEventCreate(None))
            self.dbg("落点自检: 落点@%.0f,%.0f | 笔尖->屏 %.0f,%.0f (差 %.0f,%.0f) | "
                     "抬手时光标@%.0f,%.0f"
                     % (px, py, qx, qy, px - qx, py - qy, c.x, c.y))
        if self.fire(a, px, py):
            _m, _h = self.rc_stats()
            self.say("轻点 -> %s @%.0f,%.0f (按住 %.2fs / 中位偏移 %.0fpx 峰值 %.0fpx)"
                     % (action_title("tap", a), px, py, _h, _m, self.dev_max))

    def rc_stats(self):
        """抬手时的长按判据 -> (中位偏移px, 按住秒数)。

        用中位数而不是峰值: 真人握笔「不动」时笔尖仍有 1~5px 抖动, 偶尔还有一下尖峰;
        峰值判据会让这手势永远触发不了。0.15s 之后才开始采样, 免得按下瞬间那点位移算进去。
        """
        held = time.time() - self.hold_t0
        tail = [d for t, d in self.dev_hist if (t - self.hold_t0) >= 0.15] or \
            [d for _, d in self.dev_hist]
        tail.sort()
        return (tail[len(tail) // 2] if tail else 0.0), held

    def pen_screen_anchor(self):
        """按下那一刻的笔尖坐标 -> 屏幕像素"""
        return (self.ox + self.anchor_x * self.w, self.oy + self.anchor_y * self.h)

    def begin_drag(self, which=""):
        """进入按住拖拽 —— 这时才补一个 LeftMouseDown (落在按下时的锚点), 之后按拖拽走。

        为什么要等判定才补按键: 「快速滑动」全程一个按键都不发, 应用不可能把它解释成
        划选; 只有绑成拖拽的那类手势 (默认「停住再滑」) 才升级成拖拽。
        """
        self.dragging = True
        self.last_drag = 0.0
        self.nd0 = self.nd
        if self.drag_pen:
            ax, ay = self.pen_screen_anchor()
        else:
            ax, ay = self.drag_root
        self.post(LDOWN, at=(ax, ay))
        self.n += 1
        self.say("%s -> 进入拖拽 (起点 %.0f,%.0f)" % (which or "按住", ax, ay))

    def on_move(self):
        """按下状态下的移动 (X / Y 任一事件都会进来)"""
        dx = abs(self.x - self.anchor_x) * self.w
        dy = abs(self.y - self.anchor_y) * self.h
        moved = dx if dx > dy else dy
        # 整笔的偏移曲线: 抬手时用中位数判「停住不动」(抗笔尖抖动), 峰值只进日志
        if moved > self.dev_max:
            self.dev_max = moved
        self.dev_hist.append((time.time(), moved))
        if len(self.dev_hist) > 60:
            del self.dev_hist[:-60]
        if self.dragging:
            self.drag()                 # 已进入拖拽: 之后的移动一律发 LeftMouseDragged
            return
        if self.consumed:
            return                      # 本笔已经发过完整动作 (右键/空格/返回…), 不再重复
        if not self.scrolled:
            # ★零死区: 「停住再滑」一旦成立, 笔尖再动一点点就【立刻】进入拖拽 —— 不再等
            # 12px 轻点阈值。否则"停住之后再划"的前十几像素没有任何反应, 手感就是"拖不动"。
            hs = self.bind.get("hold_swipe", "none")
            if (hs != "none" and self.hold_ok and moved >= HOLD_ARM_PX
                    and (time.time() - self.hold_t0) >= self.hold_dt):
                self.arm_gesture(hs, "停住再滑", "hold_swipe")
                return
            if moved < SCROLL_TAP_PX:
                if moved > HOLD_RADIUS_PX:
                    self.hold_ok = False    # 容差内但飘出"停住"圈 -> 这一笔不算「停住再滑」
                return                  # 还在轻点容差内, 什么都别发
            self.scrolled = True
            self.last_pen_y = self.y * self.h
            self.ns0 = self.ns
            self.arm_gesture(self.bind.get("swipe", "scroll"), "快速滑动", "swipe")
            return                      # 这一步只用来判定"这是划动", 不产生滚动量
        # 「快速滑动」绑的是滚动才继续按位移发滚轮; 绑成别的动作时, 上面那一步已经发完了
        if self.bind.get("swipe") != "scroll":
            return
        ypx = self.y * self.h
        if self.last_pen_y is None:
            self.last_pen_y = ypx
            return
        self.pend += ypx - self.last_pen_y
        self.last_pen_y = ypx
        now = time.time()
        if now - self.last_scroll >= DRAG_MIN_DT:
            self.last_scroll = now
            self.flush_scroll()

    def reset_state(self):
        """清空一笔的中间状态 (换绑定/重新开始时用, 累计计数器不动)"""
        self.down = False
        self.scrolled = False
        self.dragging = False
        self.consumed = False
        self.hold_ok = True
        self.pend = 0.0
        self.last_pen_y = None
        self.last_drag = 0.0
        self.last_scroll = 0.0

    def flush_scroll(self):
        want = self.pend * self.gain
        d = int(want)
        if not d:
            return
        self.pend -= d / self.gain   # 小数部分留着, 免得慢速划动被截断成一堆 0
        if self.scroll_flip:
            d = -d
        cg.CGEventPost(0, cg.CGEventCreateScrollWheelEvent(None, SCROLL_UNIT_PIXEL, 1, d))
        self.n += 1
        self.ns += 1
        if self.ns <= 3 or self.ns % 100 == 0:
            self.say("滚轮 %d 次 (本次 %+d px)" % (self.ns, d))


def cmd_bridge(seconds):
    takeover = "--takeover" in sys.argv
    flip = "--flip-y" in sys.argv
    drag_pen = "--drag-pen" in sys.argv
    scroll_flip = "--scroll-flip" in sys.argv
    # 绑定表: 默认就是图形界面那份 (轻点=左键单击 / 快速滑动=滚动 / 停住再滑=拖拽 / 停住不动=右键菜单)。
    # 老开关继续认: --no-hold-drag、--no-rc 等于把对应手势绑成「关」; --select 是老「滑动选择」模式。
    # 逐项改: --bind-<手势>=<动作>, 手势 tap/swipe/hold_swipe/hold, 动作见 BIND_ACTIONS。
    binds = dict(DEFAULT_BINDS)
    if "--no-hold-drag" in sys.argv:
        binds["hold_swipe"] = "none"
    if "--no-rc" in sys.argv:
        binds["hold"] = "none"
    if "--select" in sys.argv:
        binds.update(swipe="drag", hold_swipe="none", hold="none")
    for a in sys.argv:
        if a.startswith("--bind-"):
            g, _, val = a[len("--bind-"):].partition("=")
            g = g.strip().replace("-", "_")
            if g not in DEFAULT_BINDS:
                print("!! 不认识的手势 %r。可选: %s" % (g, "、".join(DEFAULT_BINDS)))
            elif val not in BIND_TITLES:
                print("!! 不认识的动作 %r。可选: %s"
                      % (val, "、".join(k for k, _ in BIND_ACTIONS)))
            else:
                binds[g] = val
    scroll = (binds.get("swipe") == "scroll") and not takeover   # 只用于下面的提示文案
    hold_ms = None
    for a in sys.argv:
        if a.startswith("--hold-drag-ms="):
            hold_ms = int(a.split("=", 1)[1])
    rc_hold_ms = RC_HOLD_DT * 1000        # 停住不动=右键菜单: 默认开
    for a in sys.argv:
        if a.startswith("--rc-hold-ms="):
            rc_hold_ms = int(a.split("=", 1)[1])
    if binds["hold"] == "none":
        rc_hold_ms = 0
    pres_thr = None
    for a in sys.argv:
        if a == "--pressure":
            pres_thr = 1
        elif a.startswith("--pressure="):
            pres_thr = int(a.split("=", 1)[1])
    if not ax.AXIsProcessTrusted():
        print("!! 缺『辅助功能』权限 —— 合成出来的鼠标事件会被系统丢掉。")
        print("   系统设置 > 隐私与安全性 > 辅助功能 -> 打开 Terminal, 完全退出后重开再跑")
    b = Bridge(takeover=takeover, drag_pen=drag_pen, binds=binds, scroll_flip=scroll_flip,
               hold_ms=hold_ms, rc_hold_ms=rc_hold_ms)
    b.set_rect(b.ox, b.oy, b.w, b.h, "跟随光标 (命令行只认系统光标那块)")
    print("按下判据: TipSwitch%s" % (" 或 压力>%d" % pres_thr if pres_thr is not None else ""))

    st = {"tip": 0, "pres": 0}
    seen = {"ev": 0, "x": 0, "y": 0, "tip": 0, "pres": 0}
    peak = [0]
    tickat = [time.time()]

    def tick():
        now = time.time()
        if now - tickat[0] >= 1.5:
            tickat[0] = now
            tail = "滚轮 %d / 轻点 %d / 拖拽 %d / 右键 %d" % (b.ns, b.nclk, b.nd, b.nrc)
            print("    [状态] 事件 %d (X %d / Y %d / TipSwitch %d / 压力 当前 %d 峰值 %d) -> 已转发 %d (%s)"
                  % (seen["ev"], seen["x"], seen["y"], seen["tip"], st["pres"], peak[0], b.n, tail))
            peak[0] = 0
            sys.stdout.flush()

    def sync():
        want = bool(st["tip"]) or (pres_thr is not None and st["pres"] > pres_thr)
        if want == b.down:
            return
        if want:
            b.press()
        else:
            if b.dragging:
                b.last_drag = 0.0    # 抬起前补发最后一段拖拽, 避免丢掉末尾点位
                b.drag()
            b.release()

    @CALLBACK
    def cb(ctx, result, sender, value):
        try:
            if not value:
                return
            el = iokit.IOHIDValueGetElement(value)
            if not el:
                return
            page = iokit.IOHIDElementGetUsagePage(el)
            usage = iokit.IOHIDElementGetUsage(el)
            v = iokit.IOHIDValueGetIntegerValue(value)
            lo = iokit.IOHIDElementGetLogicalMin(el)
            hi = iokit.IOHIDElementGetLogicalMax(el)
            if page == 0x0D and usage == 0x42:          # TipSwitch
                seen["ev"] += 1; seen["tip"] += 1
                st["tip"] = 1 if v else 0
                sync()
            elif page == 0x0D and usage == 0x30:        # TipPressure(笔接口的 0x30)
                seen["ev"] += 1; seen["pres"] += 1
                st["pres"] = v
                if v > peak[0]:
                    peak[0] = v
                sync()
            elif page == 0x0D and usage == 0x32:        # InRange
                if not v:
                    st["tip"] = 0
                    st["pres"] = 0
                    sync()
            elif page == 0x01 and usage in (0x30, 0x31) and hi > lo and lo >= 0:
                # 绝对值坐标才要 (笔); 相对坐标的鼠标接口 lo<0 会被排除
                seen["ev"] += 1
                seen["x" if usage == 0x30 else "y"] += 1
                nrm = float(v - lo) / (hi - lo)
                if flip:
                    nrm = 1.0 - nrm
                if usage == 0x30:
                    b.x = nrm
                else:
                    b.y = nrm
                if b.takeover:
                    b.post(LDRAGGED if b.down else MOVED)
                elif b.down:
                    # 按下之后的移动交给手势判定 (快速滑动=滚动 / 停住再滑=拖拽 …)。
                    # ★ 进入拖拽后必须发 kCGEventLeftMouseDragged(type 6): 只发
                    #   MouseMoved(type 5) 时应用看到的是"按下 -> 无按键的移动 -> 抬起",
                    #   于是拖拽(拖窗口/划选文字/拖滑块)全部失效、只有原地点击成立。
                    b.on_move()
            tick()
        except Exception as e:
            print("    [bridge err] %r" % (e,))

    print("桥接中 (Ctrl+C 停止)。")
    if takeover:
        print("  ① 笔悬停移动 -> 光标应跟随   ② 笔尖点图标 -> 应点中   ③ 笔尖按住拖动 -> 应能拖窗口/划选")
        print("  上下颠倒加 --flip-y; 落点整体偏移就把上面的状态行贴我, 我来加校准")
    else:
        print("  拿笔试这几件事 (按上面的绑定表):")
        print("    ① 笔尖轻点                  -> %s (抬手那一刻才确认, 按下去不会有拖影/选中)"
              % action_title("tap", binds["tap"]))
        if binds["swipe"] != "none":
            print("    ② 笔尖快速划一段            -> %s%s"
                  % (action_title("swipe", binds["swipe"]),
                     " (全程不发鼠标按键, ★ 不会选中文字)" if binds["swipe"] == "scroll" else ""))
        if binds["hold_swipe"] != "none":
            print("    ③ 笔尖先停住 %.2fs 再划       -> %s"
                  % (b.hold_dt, action_title("hold_swipe", binds["hold_swipe"])))
        if binds["hold"] != "none":
            print("    ④ 笔尖停住不动 %.2fs 再抬手   -> %s"
                  % (b.rc_hold_dt, action_title("hold", binds["hold"])))
        print("  改绑定: --bind-swipe=drag / --bind-hold=none … (动作可选: %s)"
              % "/".join(k for k, _ in BIND_ACTIONS))
        print("  停住判定太灵敏/太迟钝: --hold-drag-ms=200|250|350|500 --rc-hold-ms=600|800|1000;"
              " 方向反了加 --scroll-flip")
        print("  注意: 手指在此模式下一定无效 —— macOS 不拿手指坐标驱动光标")
    try:
        run(cb, seconds)
    except KeyboardInterrupt:
        pass
    print("\n转发事件总数: %d" % b.n)


def cmd_grant():
    """主动触发系统授权弹窗, 系统会把 Terminal 自动加进列表并高亮"""
    import os
    ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    print("当前『辅助功能』权限: %s" % ("已有" if ax.AXIsProcessTrusted() else "没有"))
    ax_request_permission(True)
    print("已发出授权请求 —— 屏幕上应弹出 『…想要控制这台电脑』, 点『打开系统设置』, 它会自动把 Terminal 加进列表并勾选。")
    time.sleep(1.5)
    if "--no-open" not in sys.argv:
        for u in ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                  "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"):
            os.system('open "%s"' % u)
            time.sleep(1)
        print("已打开『辅助功能』与『输入监控』两个面板。")
    print("若弹窗没出现: 在『隐私与安全性』列表里往下滚找『辅助功能』(或用面板搜索框搜), "
          "点列表左下的 + 手动添加 /System/Applications/Utilities/Terminal.app")


def cmd_selftest():
    """绕开 HID, 直接发鼠标移动事件, 验证 CGEvent 合成是否真的生效"""
    print("辅助功能权限 AXIsProcessTrusted = %s" % ("已有" if ax.AXIsProcessTrusted() else "没有 <-- 合成事件会被丢弃"))
    p = cg.CGEventGetLocation(cg.CGEventCreate(None))
    print("当前光标位置 = %.0f, %.0f" % (p.x, p.y))
    print("3 秒后把光标向右推 40px 再推回来 —— 盯着鼠标指针看它有没有动")
    for i in (3, 2, 1):
        print("  %d..." % i)
        sys.stdout.flush()
        time.sleep(1)
    for s in range(12):
        cg.CGEventPost(0, cg.CGEventCreateMouseEvent(None, MOVED, CGPoint(p.x + 40.0 * (s + 1) / 12.0, p.y), 0))
        time.sleep(0.02)
    time.sleep(0.4)
    for s in range(12):
        cg.CGEventPost(0, cg.CGEventCreateMouseEvent(None, MOVED, CGPoint(p.x + 40.0 * (11 - s) / 12.0, p.y), 0))
        time.sleep(0.02)
    print("做完 24 次合成移动。指针动了 => CGEvent 通道可用; 完全没动 => 权限没真正生效。")


def main():
    m = sys.argv[1] if len(sys.argv) > 1 else "help"
    num = lambda i, d: float(sys.argv[i]) if len(sys.argv) > i and sys.argv[i].replace(".", "").isdigit() else d
    if m == "scan":
        run(None, 0.3)
    elif m == "grant":
        cmd_grant()
    elif m == "selftest":
        cmd_selftest()
    elif m == "listen":
        cmd_listen(num(2, 20.0), len(sys.argv) > 3 and sys.argv[3] == "raw")
    elif m == "probe":
        cmd_probe(num(2, 12.0))
    elif m == "finger":
        cmd_finger(num(2, 12.0))
    elif m == "bridge":
        cmd_bridge(num(2, 600.0))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
