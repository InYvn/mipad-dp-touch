#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入模式 / 触摸+笔接口 的离线自检。

钉住三件事:
  1. 匹配表按模式给出正确的 usage 集合（pen=0x02 / touch=0x04 / auto=两个都认）
  2. 「触摸+笔」接口的事件流按【实测时序】处理:
       · 按下: Y, X, InRange=1, TipSwitch=1 —— 坐标先到、按下标志最后到
       · 抬手: Y=0, X=0, InRange=0, TipSwitch=0 —— 先发一对 (0,0) 归零
     所以坐标先缓冲、成对落位；按下时立刻按（光标已就位）；抬手那对 (0,0) 必须丢掉。
     多指只采用第 1 根手指。
  3. 老的纯笔路径一字不改 —— 传 5 个参数的 feed() 必须还是老行为。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import hid_bridge as HB          # noqa: E402
import dptouch_engine as E       # noqa: E402

FAILED = []
N = [0]


def check(name, got, want):
    N[0] += 1
    if got != want:
        FAILED.append((name, want, got))
        print("  ✗ %s\n     期望 %r\n     实际 %r" % (name, want, got))


def check_true(name, ok, detail=""):
    N[0] += 1
    if not ok:
        FAILED.append((name, True, False))
        print("  ✗ %s" % name)
        if detail:
            print("     (context: %r)" % (detail,))


class FakeBridge(object):
    """只记事件，不碰 CoreGraphics。"""

    def __init__(self):
        self.x = self.y = 0.5
        self.down = False
        self.dragging = False
        self.takeover = False
        self.events = []
        self.posts = []            # [(kind, use_pen, at)] —— 记录坐标来源, 见下面那条断言
        self.n = self.ns = self.nd = self.nclk = 0
        self.last_drag = 0.0

    def post(self, kind, use_pen=None, at=None):
        self.events.append(("post", kind))
        self.posts.append((kind, use_pen, at))

    def press(self):
        self.down = True
        self.nclk += 1
        self.events.append(("press",))

    def release(self):
        self.down = False
        self.events.append(("release",))

    def on_move(self):
        self.events.append(("move",))

    def drag(self):
        self.events.append(("drag",))

    def dbg(self, *a, **kw):
        pass

    def reset_state(self):
        pass


def main():
    print("== 一、模式与匹配表 ==")
    check("INPUT_MODES 三项", list(HB.INPUT_MODES), ["auto", "pen", "touch"])
    check("默认模式标题", HB.input_mode_title("auto"), "自动（两种都认）")
    check("未知模式回退到 auto 标题", HB.input_mode_title("nope"), "自动（两种都认）")
    check("pen 只认 0x02", list(HB.match_usages("pen")), [HB.PEN_USAGE])
    check("touch 只认 0x04", list(HB.match_usages("touch")), [HB.TOUCH_USAGE])
    check("auto 两个都认（笔在前）", list(HB.match_usages("auto")),
          [HB.PEN_USAGE, HB.TOUCH_USAGE])

    eng = E.Engine()
    check("引擎默认 input_mode = auto（两种数据格式都认）",
          eng.cfg.get("input_mode"), "auto")
    for mode, want_n in (("pen", 2), ("touch", 2), ("auto", 4)):
        with eng._lk:
            eng.cfg["input_mode"] = mode
            arr = eng._match()
        n = HB.cf.CFArrayGetCount(arr)
        HB.cf.CFRelease(arr)
        check("%s 模式: 匹配表 %d 条 (2 VID × %d usage)" % (mode, want_n, want_n // 2),
              n, want_n)

    print("\n== 二、触摸+笔接口的事件流（按实测时序）==")
    with eng._lk:
        eng.b = FakeBridge()
        eng._touch_cookies = {10, 11, 12, 13, 14}      # 第 1 根手指的 5 个元素
        b = eng.b
    TOUCH, PEN = E.TOUCH_REPORT_ID, E.PEN_REPORT_ID

    # 按下: Y -> X -> InRange=1 -> TipSwitch=1
    eng.feed(0x01, 0x31, 16384, 0, 32767, TOUCH, 13)          # Y 先到
    check("单轴坐标先到 -> 只缓冲, 不落位", b.events, [])
    eng.feed(0x01, 0x30, 8192, 0, 32767, TOUCH, 10)           # X 到齐
    check_true("Y/X 成对 -> 光标落位", ("post", HB.MOVED) in b.events, b.events)
    check("落位必须显式带坐标 (use_pen=True) —— 否则 post 会发到当前光标位置=不动",
          [(k, up) for (k, up, _at) in b.posts], [(HB.MOVED, True)])
    check("落位到 X 值", round(b.x, 4), round(8192 / 32767.0, 4))
    check("落位时还没按下", b.down, False)
    eng.feed(0x0D, 0x32, 1, 0, 1, TOUCH, 12)                  # InRange=1
    check("InRange=1 不产生按下", b.down, False)
    eng.feed(0x0D, 0x42, 1, 0, 1, TOUCH, 12)                  # TipSwitch=1
    check("TipSwitch=1 -> 立刻按下（光标已就位）", b.down, True)

    b.events[:] = []
    eng.feed(0x01, 0x31, 17500, 0, 32767, TOUCH, 13)          # 按下中移动
    eng.feed(0x01, 0x30, 9000, 0, 32767, TOUCH, 10)
    check_true("按下中移动 -> post(MOVED) + on_move（交给手势判定）",
               ("post", HB.MOVED) in b.events and ("move",) in b.events, b.events)

    b.events[:] = []
    eng.feed(0x01, 0x30, 100, 0, 32767, TOUCH, 99)            # 第 2 根手指
    eng.feed(0x0D, 0x42, 1, 0, 1, TOUCH, 99)
    check("非第 1 根手指的事件被忽略", b.events, [])
    check("非首指不改动按下状态", b.down, True)

    b.events[:] = []
    before_x = b.x
    eng.feed(0x01, 0x31, 0, 0, 32767, TOUCH, 13)              # 抬手: 固件先发 (0,0)
    eng.feed(0x01, 0x30, 0, 0, 32767, TOUCH, 10)
    check("抬手那对 (0,0) 被丢掉（不甩光标到左上角）", b.events, [])
    check("坐标没有被归零", round(b.x, 4), round(before_x, 4))
    eng.feed(0x0D, 0x32, 0, 0, 1, TOUCH, 12)
    eng.feed(0x0D, 0x42, 0, 0, 1, TOUCH, 12)
    check("抬手 -> release", b.down, False)

    # ★回归（2026-10-15 用户实测）: 抬手前最后一份 report 只动了 X 时, X 会留在缓冲里;
    #   抬手先到的 Y=0 若跟它配对, 光标就"啪"地跳到屏幕最顶部, release() 补发的那段拖拽
    #   也停在顶部 —— 所以两轴必须是同一份 report 里到的才准配对。
    with eng._lk:
        eng.b = FakeBridge()
        eng._touch_cookies = {10, 11, 12, 13, 14}
        b = eng.b
    b.down = True
    b.x, b.y = 0.62, 0.44                                    # 上一份 report 落下的位置
    eng.feed(0x01, 0x30, 20000, 0, 32767, TOUCH, 10)         # 只动了 X -> 单轴缓冲
    time.sleep(0.02)                                         # 隔半拍（下一份 report 才到）
    b.events[:] = []
    eng.feed(0x01, 0x31, 0, 0, 32767, TOUCH, 13)             # 抬手: 固件先发 Y=0
    check("残值 X 不跟抬手的 Y=0 配对（不跳到屏幕顶部）", b.events, [])
    check("Y 没有被归零", round(b.y, 4), 0.44)
    eng.feed(0x0D, 0x42, 0, 0, 1, TOUCH, 12)                 # 抬手标志
    check("抬手后也没有多出移动",
          [e for e in b.events if e[0] in ("post", "move", "drag")], [])
    check("抬手 -> release", b.down, False)

    # 反过来: 手指真的滑到屏幕上边缘 -> 下一份 report 补落位, 不能因为防跳变就丢掉跟踪
    with eng._lk:
        eng.b = FakeBridge()
        eng._touch_cookies = {10, 11, 12, 13, 14}
        b = eng.b
    b.down = True
    b.x, b.y = 0.62, 0.44
    eng.feed(0x01, 0x30, 20000, 0, 32767, TOUCH, 10)         # 单轴缓冲
    time.sleep(0.02)
    b.events[:] = []
    eng.feed(0x01, 0x31, 0, 0, 32767, TOUCH, 13)             # 到上边缘: Y=0（先挂着）
    check("边缘的 y=0 当份不落位（跟抬手同一形状, 先观察一份）", b.events, [])
    eng.feed(0x01, 0x30, 24000, 0, 32767, TOUCH, 10)         # 手指还在动 -> 补落位
    check_true("滑到边缘的 y=0 会在下一份 report 补落位",
               ("post", HB.MOVED) in b.events, b.events)
    check("补落位的 Y = 0（真的在屏幕顶边）", round(b.y, 4), 0.0)
    check("补落位的 X 用新值", round(b.x, 4), round(24000 / 32767.0, 4))

    # 元素枚举失败时（首指 cookie 集为空）退化成"全都收", 不能整条触控失灵
    with eng._lk:
        eng._touch_cookies = set()
        b.events[:] = []
    eng.feed(0x01, 0x30, 4096, 0, 32767, TOUCH, 99)
    eng.feed(0x01, 0x31, 4096, 0, 32767, TOUCH, 99)
    check_true("识别失败时退化为全收（不至于整条触控失灵）",
               ("post", HB.MOVED) in b.events, b.events)

    print("\n== 三、老的纯笔路径一字不改 ==")
    with eng._lk:
        eng.b = FakeBridge()
        b = eng.b
    eng.feed(0x01, 0x30, 16384, 0, 32767)                     # 5 参数 = 老签名
    check("笔: 未接管坐标时悬停不动光标", b.events, [])
    eng.feed(0x0D, 0x42, 1, 0, 1)                             # 笔尖按下
    check("笔: 按下", b.down, True)
    eng.feed(0x0D, 0x42, 0, 0, 1)
    check("笔: 抬起", b.down, False)

    with eng._lk:
        b = FakeBridge()
        b.takeover = True
        eng.b = b
    eng.feed(0x01, 0x30, 16384, 0, 32767)
    check_true("笔: 接管坐标后悬停也动光标", ("post", HB.MOVED) in b.events, b.events)

    print("\n== 四、首指 cookie 布局（按设备实测的元素排列，2026-10-14）==")
    # 元素是按【用途】分组的, 不是按手指分组 —— 直接照抄 enumerate_touch.py 的实测结果
    rows = []
    for i in range(10):                                  # 第 1..10 指: TipSwitch/InRange 对
        rows.append((22 + 2 * i, 1, 0x0D, 0x42))
        rows.append((23 + 2 * i, 1, 0x0D, 0x32))
    for ck, u in ((42, 0x42), (43, 0x44), (44, 0x45), (45, 0x32)):   # 笔 (report ID 3)
        rows.append((ck, 3, 0x0D, u))
    for i in range(10):                                  # 第 1..10 指: ContactID, X, Y
        base = 46 + 3 * i
        rows.append((base, 1, 0x0D, 0x51))
        rows.append((base + 1, 1, 0x01, 0x30))
        rows.append((base + 2, 1, 0x01, 0x31))
    rows.sort()
    got = E.first_finger_cookies_from(rows)
    check("首指 cookie = {22,23,46,47,48}（不是 cookie 最小的 5 个）",
          sorted(got), [22, 23, 46, 47, 48])
    check("没把第 2 指的 TipSwitch/InRange 混进来", sorted(got & {24, 25}), [])
    check("纯笔接口（只有 report ID 3）-> 空集",
          E.first_finger_cookies_from([(1, 3, 0x0D, 0x42), (2, 3, 0x0D, 0x32)]), set())

    print("\n== 五、触摸落点必须来自触摸坐标（不能读系统光标位置）==")
    # 真 Bridge（press() 不发任何事件, 所以这里安全）: 触摸时落点 = 我们手里的触摸坐标;
    # 笔仍按老路 —— 取按下瞬间的系统光标。
    rb = HB.Bridge(takeover=False)
    rb.ox, rb.oy, rb.w, rb.h = 100.0, 50.0, 1000.0, 800.0
    rb.x, rb.y = 0.5, 0.5                                  # -> 屏上 (600, 450)
    rb.src_touch = True
    rb.press()
    check("触摸按下: 落点 = 触摸坐标", rb.tap_pos, (600.0, 450.0))
    check("触摸按下: 拖拽基准也取触摸坐标", rb.drag_root, (600.0, 450.0))
    rb.src_touch = False
    rb.press()
    _q = HB.cg.CGEventGetLocation(HB.cg.CGEventCreate(None))
    check("笔按下: 落点仍是按下瞬间的系统光标", rb.tap_pos, (_q.x, _q.y))
    rb.takeover = True
    rb.press()
    _q2 = HB.cg.CGEventGetLocation(HB.cg.CGEventCreate(None))
    check("笔 + 接管坐标: 落点仍是系统光标（原生就跟着笔走, 不动老契约）",
          rb.tap_pos, (_q2.x, _q2.y))

    print("\n" + "-" * 64)
    if FAILED:
        print("失败 %d 项 / 通过 %d 项" % (len(FAILED), N[0] - len(FAILED)))
        return 1
    print("全部通过 (%d 项断言)" % N[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
