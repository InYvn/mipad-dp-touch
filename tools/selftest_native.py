#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生触控模式的引擎回归自检 —— 离线, 不碰真 HID。

钉住的行为:
  1. 原生触控开: 手指 (触摸接口, report ID 1) 的一切 —— 点击 / 移动 / 滚动 /
     捏合 —— **一个合成 CGEvent 都不发**; 计数照记 (输入确实收到了)。
  2. 原生触控开: 笔 (report ID 3) 路径一字不改 —— 轻点 / 拖拽 / 滚动照常合成。
     (macOS 系统负责手指手势, 桥接只服务笔; 两边绝不能双发。)
  3. 原生触控关 (老行为): 手指事件照常合成 (回归护栏)。
  4. 运行中切换开关: 半截手指手势 (双指/按压) 立即复位, 残留不带进原生/桥接。
  5. 最小真实 Engine 事件驱动: 用真 Engine 走一笔真事件, 抓缺失属性 /
     异常 (不启动 IOHIDManager, 不碰权限)。

用法: python3 tools/selftest_native.py   (全过 exit 0, 有失败 exit 1)
"""
import os
import sys

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
        print("  ✗ %s%s" % (name, " | " + detail if detail else ""))


class Recorder(object):
    """把 hid_bridge 的 CGEvent 合成函数换成录音机 (与 selftest_bindings 同法)。"""

    def __init__(self):
        self.ev = []

    def install(self):
        cg = HB.cg
        rec = self

        def mouse(_a, kind, pt, _b):
            rec.ev.append(("mouse", kind, (pt.x, pt.y)))

        def key(_a, code, down):
            rec.ev.append(("key", code, bool(down)))

        def scroll(_a, _u, _n, delta):
            rec.ev.append(("scroll", delta))

        cg.CGEventCreateMouseEvent = mouse
        cg.CGEventCreateKeyboardEvent = key
        cg.CGEventCreateScrollWheelEvent = scroll
        # Final posting boundary too: Bridge.click/fire bypass Bridge.post.
        cg.CGEventPost = lambda *a: None
        cg.CGEventSetIntegerValueField = lambda *a: None
        return self


def _pen_click(eng):
    """笔: 移到 (0.5,0.5) -> 按下 -> 抬起 (report ID 3 = 纯笔接口)。"""
    eng.feed(0x01, 0x30, 500, 0, 1000)
    eng.feed(0x01, 0x31, 500, 0, 1000)
    eng.feed(0x0D, 0x42, 1, 0, 1)
    eng.feed(0x0D, 0x42, 0, 0, 1)


def _finger_click(eng):
    """手指: Y, X 先到, InRange=1, TipSwitch=1 最后 (固件实测顺序, report ID 1)。"""
    eng.feed(0x01, 0x31, 400, 0, 1000, rid=1, cookie=60)
    eng.feed(0x01, 0x30, 600, 0, 1000, rid=1, cookie=61)
    eng.feed(0x0D, 0x32, 1, 0, 1, rid=1, cookie=50)
    eng.feed(0x0D, 0x42, 1, 0, 1, rid=1, cookie=50)
    eng.feed(0x0D, 0x42, 0, 0, 1, rid=1, cookie=50)


def t_native_finger_silent():
    print("== 1: 原生触控开 -> 手指不合成任何 CGEvent ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="native")
    for _ in range(3):
        _finger_click(eng)
    check("手指事件后录音机为空", rec.ev, [])
    st = eng.status()
    check_true("手指输入仍被计数 (收到但不转发)", st["events"] > 0,
               "events=%d" % st["events"])
    check("转发计数为 0", st["fwd"], 0)
    check("status 报 native_touch=True", st.get("native_touch"), True)


def t_pen_still_routes():
    print("== 2: 原生触控开 -> 笔路径一字不改 ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="native")
    _pen_click(eng)
    kinds = [e[1] for e in rec.ev]
    check_true("笔按下合成 LDOWN", 1 in kinds, str(rec.ev))
    check_true("笔抬起合成 LUP", 2 in kinds, str(rec.ev))
    check("转发计数 == 录音条数", eng.status()["fwd"], len(rec.ev))


def t_legacy_regression():
    print("== 3: 原生触控关 -> 手指照常合成 (老行为护栏) ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="legacy", legacy_touch_ack=True)
    _finger_click(eng)
    check_true("手指点击照常合成", any(e[0] == "mouse" for e in rec.ev), str(rec.ev))


def t_midgesture_toggle():
    print("== 4: 运行中切开关 -> 半截手指手势复位 ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="legacy", legacy_touch_ack=True)
    # 手指按下, 不抬起 (半截手势)
    eng.feed(0x0D, 0x42, 1, 0, 1, rid=1, cookie=50)
    eng.update(touch_mode="native")
    check("半截按压被丢弃 (bridge 不处于按下)", eng.b.down, False)
    rec.ev.clear()
    _finger_click(eng)
    check("切换后手指事件仍静默", rec.ev, [])
    # 笔这边完全不受影响
    _pen_click(eng)
    check_true("切换后笔仍能点", any(e[1] == 1 for e in rec.ev), str(rec.ev))
    # 切回桥接模式: 手指又能用了
    eng.update(touch_mode="legacy", legacy_touch_ack=True)
    rec.ev.clear()
    _finger_click(eng)
    check_true("切回后手指恢复合成", any(e[0] == "mouse" for e in rec.ev), str(rec.ev))


def t_finger_pinch_scroll_silent():
    print("== 5: 原生触控开 -> 双指滚动/捏合也不合成 ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="native")
    # 双指: 第 1/2 指按下 + 移动 (rid=1, 各自 cookie)
    for ck in (50, 51):
        eng.feed(0x0D, 0x42, 1, 0, 1, rid=1, cookie=ck)
    for ck, x in ((60, 400), (62, 500)):
        eng.feed(0x01, 0x30, x, 0, 1000, rid=1, cookie=ck)
    for ck, y in ((61, 400), (63, 500)):
        eng.feed(0x01, 0x31, y, 0, 1000, rid=1, cookie=ck)
    for _ in range(4):          # 质心移动几帧
        eng.feed(0x01, 0x30, 420, 0, 1000, rid=1, cookie=60)
        eng.feed(0x01, 0x31, 420, 0, 1000, rid=1, cookie=61)
    check("双指期间零合成事件", rec.ev, [])


def t_toggle_releases_ongoing_synthetic_gesture():
    from unittest.mock import patch
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)
    eng.touch_iface = True
    eng.update(touch_mode="legacy", legacy_touch_ack=True)
    eng.b.src_touch = True
    eng.b.down = True
    eng.b.dragging = True
    eng._pinch_active = True
    eng._latch_until = 1e20
    eng._two_pend = [100, 100]
    with patch.object(eng.b, "pinch_native", return_value=True) as pinch:
        eng.update(touch_mode="native")
        check_true("切换原生前释放合成拖拽", any(e[0] == "mouse" and e[1] == 2 for e in rec.ev))
        check_true("切换前结束已开始的捏合", pinch.called)
    check("切换清空旧手势 latch", eng._latch_until, 0.0)
    check("切换清空待发滚动", eng._two_pend, [0.0, 0.0])


def t_real_engine_minimal():
    print("== 6: 最小真实 Engine 事件驱动 (抓缺失属性/异常) ==")
    rec = Recorder().install()
    eng = E.Engine(log=lambda m: None)     # 真 Engine: 真 Bridge / 真状态机
    eng.touch_iface = True
    eng.update(touch_mode="native")
    # 真走一笔混合事件: 手指 + 笔, 断言不抛异常且计数/状态齐全
    _finger_click(eng)
    _pen_click(eng)
    _finger_click(eng)
    st = eng.status()
    for key in ("running", "rc", "ax", "devices", "events", "fwd", "scroll",
                "click", "drag", "rclick", "down", "binds", "target",
                "target_takeover", "flip", "error", "native_touch", "touch_iface"):
        check_true("status 有键 %s" % key, key in st)
    check_true("手指未转发, 笔已转发", st["fwd"] > 0 and
               all(not e[0].startswith("touch") for e in []), "fwd=%d" % st["fwd"])
    check("引擎不处于按下", st["down"], False)


def main():
    print("原生触控模式引擎回归自检")
    for fn in (t_native_finger_silent, t_pen_still_routes, t_legacy_regression,
               t_midgesture_toggle, t_finger_pinch_scroll_silent, t_toggle_releases_ongoing_synthetic_gesture, t_real_engine_minimal):
        try:
            fn()
        except Exception as e:
            import traceback
            print("  !! 组内异常: %r\n%s" % (e, traceback.format_exc()))
            FAILED.append((fn.__name__, "不抛异常", repr(e)))
    print("共 %d 项断言, 失败 %d" % (N[0], len(FAILED)))
    for name, want, got in FAILED:
        print("  ✗ %s | 期望 %r 实际 %r" % (name, want, got))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
