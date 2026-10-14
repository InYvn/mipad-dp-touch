#!/usr/bin/env python3
"""离线自检: 笔按下之后「移动」到底有没有变成真的拖拽 / 滚动。

★它专门盯一个已经踩过的坑 (2026-09-18 用户实测发现):
    dptouch_engine._feed_locked 处理「按下 + 移动」时读过一个不存在的 b.scroll 属性
    -> 每移动一下就抛 AttributeError, 被 HID 回调里的 except 吞掉, 日志里只留一行
    「事件回调异常」。表现: 能轻点、能停住不动出右键菜单, 但**拖不动窗口、划不了选、
    快速滑动也滚不动**。

    为什么 tools/selftest_bindings.py 没抓到: 它直接调 b.on_move(), 绕开了引擎那一层。
    所以这里走真正的引擎路径 —— 借 Engine._feed_locked, 喂一串 HID 报告进去, 看合成
    出来的鼠标事件序列对不对; 一旦引擎里再冒异常, 这里会直接炸出来, 不会被 except 吞。

覆盖:
    1. 停住再滑 -> 拖拽: 有 LeftMouseDown, 有多个位置**真的在动**的 LeftMouseDragged, 收尾有 Up
    2. 快速滑动 + 滚动: 全程不发任何鼠标按键, 只有滚轮
    3. 快速滑动 + 拖拽: 就是「划选文字」, 按键起了而且在动 (停住再滑不再被说成选中文字)
    4. 接管坐标 (光标基准屏 = 平板): 悬停把光标挪过去, 按下后的拖拽跟着笔的绝对位置走
    5. 拖拽的两种参考点: 默认相对按下时光标 / 勾了「拖拽位置跟随笔尖」用笔尖绝对位置
    6. 引擎整条路径 0 异常 (上面那个 AttributeError 的回归测试)

用法: .venv/bin/python tools/selftest_drag.py   (退出码 0 = 全过)
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import dptouch_engine as EN          # noqa: E402
import hid_bridge as H               # noqa: E402

CG_Y0 = 700.0                        # 自检里假装系统光标停在 (1200, 700)
CG_X0 = 1200.0

FAIL = []
N_OK = 0


def ok(cond, what):
    global N_OK
    if cond:
        N_OK += 1
        print("  ✓ %s" % what)
    else:
        FAIL.append(what)
        print("  ✗ %s" % what)


# ---------------------------------------------------------------- 记录型 Bridge
class RecBridge(H.Bridge):
    """真 Bridge, 只是把「发事件」换成「记下来」。判断逻辑一行都不改。"""

    def __init__(self, **kw):
        self.ev = []
        super().__init__(**kw)

    def post(self, kind, use_pen=None, at=None):
        if at is None:                      # 悬停 MOVED: 真实现就是把笔尖铺到基准屏上
            at = (self.ox + self.x * self.w, self.oy + self.y * self.h)
        self.ev.append(("POST", kind, at))

    def flush_scroll(self):
        """照抄真实现, 只是不发事件 (自检进程本来也没辅助功能权限)"""
        d = int(self.pend * self.gain)
        if not d:
            return
        self.pend -= d / self.gain
        if self.scroll_flip:
            d = -d
        self.ev.append(("SCROLL", d))
        self.n += 1
        self.ns += 1

    def click(self, px, py, clicks=1):
        self.ev.append(("CLICK", px, py))

    def right_click(self, px=None, py=None):
        self.ev.append(("RIGHT", px, py))

    def key_tap(self, code, cmd=False):
        self.ev.append(("KEY", code))

    # 便于断言的小工具
    def kinds(self, k):
        return [e for e in self.ev if e[0] == "POST" and e[1] == k]

    def drag_xy(self):
        return [(round(x), round(y)) for _k, _c, (x, y) in self.kinds(H.LDRAGGED)]


class Eng:
    """Engine 的无头替身: 借真正的 _feed_locked / _sync / _dbg_hover_line。"""

    _feed_locked = EN.Engine._feed_locked
    _sync = EN.Engine._sync
    _dbg_hover_line = lambda self, b: ""          # noqa: E731

    def __init__(self, b):
        class Seen(dict):
            def __missing__(self, k):
                self[k] = 0
                return 0

        self.b = b
        self.seen = Seen()
        self.total = Seen()
        self._want_down = False
        self.pres_cur = 0
        self.pres_peak = 0
        self.debug = False

    # --- 喂 HID 报告 ---
    def tip(self, down):
        self._feed_locked(0x0D, 0x42, 1 if down else 0, 0, 1)

    def pos(self, nx, ny):
        self._feed_locked(0x01, 0x30, int(nx * 32767), 0, 32767)
        self._feed_locked(0x01, 0x31, int(ny * 32767), 0, 32767)


def new_eng(**kw):
    b = RecBridge(**kw)
    b.set_rect(0.0, 0.0, 2560.0, 1440.0, "主屏 2560x1440")   # 显式基准, 不碰主屏探测
    return Eng(b), b


def hold(sec):
    time.sleep(sec)


print("=== 0. 光标位置桩 (自检进程没有屏幕, 假装光标停在 %.0f,%.0f) ===" % (CG_X0, CG_Y0))
H.cg.CGEventGetLocation = lambda e: types.SimpleNamespace(x=CG_X0, y=CG_Y0)
ok(True, "CGEventGetLocation 已替换 (只影响本进程)")

# ------------------------------------------------------------------ 1. 停住再滑
print("\n=== 1. 停住再滑 -> 拖拽 (默认绑定) ===")
eng, b = new_eng(binds=dict(H.DEFAULT_BINDS), hold_ms=250)
eng.pos(0.50, 0.50)                          # 真笔是先有位置(悬停)才按下 —— 顺序别反
eng.tip(True)
hold(0.30)                                  # 停住超过 250ms —— 这一笔已经是「停住再滑」
for i in range(1, 6):                       # 之后往右下划 5 步
    eng.pos(0.50 + 0.02 * i, 0.50 + 0.01 * i)
    hold(0.02)
eng.tip(False)

downs = b.kinds(H.LDOWN)
drags = b.drag_xy()
ups = b.kinds(H.LUP)
ok(len(downs) == 1, "只补了一个 LeftMouseDown (判定前不发按键): %d 个" % len(downs))
ok(len(drags) >= 4, "拖拽事件有 %d 个 (笔在动就得跟着发)" % len(drags))
ok(len(ups) == 1, "收尾有 LeftMouseUp: %d 个" % len(ups))
if len(drags) >= 2:
    sp = max(abs(a[0] - c[0]) + abs(a[1] - c[1]) for a in drags for c in drags)
    ok(sp > 50, "拖拽落点在动 (最远两点差 %dpx, ★ 冻在原地 = 拖不动)" % sp)
    ok(drags[-1][0] >= drags[0][0] and drags[-1][1] >= drags[0][1],
       "拖拽只往笔尖的方向推进, 没有回跳: %r -> %r" % (drags[0], drags[-1]))
ok(drags and 1150 < drags[0][0] < 1300, "起点落在按下时的光标附近 (%r) —— 和轻点一致" % (drags[0],))

# --------------------------------------------------------------- 2. 快速滑动滚动
print("\n=== 2. 快速滑动 -> 滚动 (不停住, 起手就划) ===")
eng, b = new_eng(binds={"tap": "left", "swipe": "scroll", "hold_swipe": "none",
                        "hold": "right"}, hold_ms=250)
eng.pos(0.50, 0.50)
eng.tip(True)
for i in range(1, 7):
    eng.pos(0.50, 0.50 - 0.03 * i)          # 往上划 = 翻页
    hold(0.012)
eng.tip(False)
ok(not b.kinds(H.LDOWN), "全程一个鼠标按键都没发 (★ 不会误选文字): %d" % len(b.kinds(H.LDOWN)))
ok(len(b.kinds(H.LDRAGGED)) == 0, "也没有拖拽事件")
ok(len([e for e in b.ev if e[0] == "SCROLL"]) > 0,
   "滚轮发了 %d 次" % len([e for e in b.ev if e[0] == "SCROLL"]))

# --------------------------------------------------- 3. 快速滑动 -> 拖拽(划选)
print("\n=== 3. 快速滑动 -> 拖拽 = 划选文字 (起手就划, 不等停住) ===")
eng, b = new_eng(binds={"tap": "left", "swipe": "drag", "hold_swipe": "none",
                        "hold": "right"}, hold_ms=250)
eng.pos(0.50, 0.50)
eng.tip(True)
for i in range(1, 7):
    eng.pos(0.50 + 0.02 * i, 0.50)
    hold(0.012)
eng.tip(False)
ok(len(b.kinds(H.LDOWN)) == 1, "起手 12px 之后才补 Down: %d 个" % len(b.kinds(H.LDOWN)))
ok(len(b.drag_xy()) >= 4, "划选过程中有 %d 个拖拽点" % len(b.drag_xy()))
ok(H.action_title("swipe", "drag") == "拖拽 · 选中文字",
   "快速滑动的拖拽叫「%s」" % H.action_title("swipe", "drag"))
ok(H.action_title("hold_swipe", "drag") == "拖拽 · 移动窗口",
   "停住再滑的拖拽叫「%s」(不再说成选中文字)" % H.action_title("hold_swipe", "drag"))
ok(H.action_title("tap", "left") == "左键单击", "没有特指的手势用通用名: 轻点 -> %s"
   % H.action_title("tap", "left"))

# ------------------------------------------------------------- 4. 接管坐标 (跨屏)
print("\n=== 4. 光标基准屏 = 平板 (接管坐标, 笔能把光标带过去) ===")
eng, b = new_eng(binds=dict(H.DEFAULT_BINDS), hold_ms=250)
b.set_rect(2560.0, 0.0, 1704.0, 1136.0, "平板 MI DISPLAY")   # 平板在主屏右边
b.takeover = True
eng.pos(0.50, 0.50)                          # 悬停
ok(len(b.kinds(H.MOVED)) >= 1, "悬停就把光标挪过去 (接管模式下笔就是鼠标): %d 次"
   % len(b.kinds(H.MOVED)))
mv = b.kinds(H.MOVED)[0][2]
ok(2560 < mv[0] < 4264, "光标落在平板那块屏的矩形里 (%r)" % (mv,))
eng.tip(True)
hold(0.30)                                   # 停住 -> 这一笔按「停住再滑」判定, 不会变成滚动
eng.pos(0.60, 0.60)
hold(0.02)
eng.tip(False)
d = b.drag_xy()
ok(d and 2560 < d[-1][0] < 4264, "拖拽也铺在平板上 (%r) —— 笔跨屏后拖拽照样能用" % (d[-1:],))

# --------------------------------------------------- 5. 两种拖拽参考点 / 6. 无异常
print("\n=== 5. 拖拽参考点: 默认相对光标 / 「跟随笔尖」用绝对位置 ===")
eng, b = new_eng(binds=dict(H.DEFAULT_BINDS), hold_ms=250)
eng.pos(0.10, 0.10)                          # 笔尖在屏幕左上角 (256,144), 光标在 (1200,700)
eng.tip(True)
hold(0.30)
eng.pos(0.14, 0.12)
hold(0.02)
eng.tip(False)
rel = b.drag_xy()
ok(rel and abs(rel[0][0] - CG_X0) < 250 and abs(rel[0][1] - CG_Y0) < 250,
   "默认: 从按下时的光标附近开始拖 (%r, 笔尖自己的绝对位置是 358,171)" % (rel[0],))

eng, b = new_eng(binds=dict(H.DEFAULT_BINDS), hold_ms=250, drag_pen=True)
eng.pos(0.10, 0.10)
eng.tip(True)
hold(0.30)
eng.pos(0.14, 0.12)
hold(0.02)
eng.tip(False)
abso = b.drag_xy()
ok(abso and abs(abso[-1][0] - 358) < 20 and abs(abso[-1][0] - CG_X0) > 500,
   "跟随笔尖: 拖拽落在笔尖自己的位置上 (%r), 与光标无关" % (abso[-1:],))

print("\n=== 6. 引擎整条路径 0 异常 (b.scroll 那个崩的回归测试) ===")
eng, b = new_eng(binds=dict(H.DEFAULT_BINDS), hold_ms=250)
boom = None
try:
    eng.pos(0.3, 0.3)
    eng.tip(True)
    for i in range(1, 8):                    # 按下之后每一动都要走完引擎
        eng.pos(0.3 + 0.01 * i, 0.3)
        hold(0.01)
    eng.tip(False)
except Exception as e:                       # noqa: BLE001
    boom = e
ok(boom is None, "按下 + 移动没有抛异常 (真机上这里抛过一次 AttributeError: %r)" % (boom,))

print("\n%s  (%d 项通过%s)" % ("全部通过" if not FAIL else "有失败项",
                              N_OK, "" if not FAIL else ", %d 项失败" % len(FAIL)))
for f in FAIL:
    print("  !! %s" % f)
sys.exit(1 if FAIL else 0)
