#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按键绑定离线自检 —— 不碰真 HID、不合成真事件, 只拦下"到底发了什么"。

为什么必须有这个: 手势判定 + 动作派发是「我改我看不见」的东西 (要真笔真屏才能体感),
但**发出去的事件序列**可以在进程内拦下来逐条比对 —— 轻点会不会留下拖影、停住再滑
到底有没有 LeftMouseDragged、双击带不带 ClickState, 全都钉得住。

做法: 把 hid_bridge 用的那几个 CoreGraphics 合成函数换成录音机, 然后手工"走"一笔
(按下 -> 移动 -> 抬手), 断言录到的序列。时间靠回拨 hold_t0 来伪造, 不真等。

用法: python3 tools/selftest_bindings.py     (全过 exit 0, 有失败 exit 1)
"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import hid_bridge as HB            # noqa: E402

MOUSE_KIND = {1: "LDOWN", 2: "LUP", 3: "RDOWN", 4: "RUP", 5: "MOVED", 6: "LDRAGGED",
              25: "MDOWN", 26: "MUP"}
FIELD = {HB.CLICK_STATE_FIELD: "click", HB.BUTTON_NUM_FIELD: "btn"}


class _Pt(object):
    def __init__(self, x, y):
        self.x, self.y = x, y


class Recorder(object):
    """把 CGEvent* 合成函数换成录音机 (原地塞进 hid_bridge.cg)。"""

    def __init__(self, cursor=(100.0, 200.0)):
        self.ev = []
        self.cursor = cursor

    def install(self):
        cg = HB.cg
        rec = self

        def mouse(_a, kind, pt, _b):
            return ["mouse", kind, (pt.x, pt.y), {}]

        def key(_a, code, down):
            return ["key", code, bool(down), {}]

        def scroll(_a, _unit, _n, delta):
            return ["scroll", delta]

        def field(ev, f, val):
            try:
                ev[3][f] = val
            except Exception:
                pass

        cg.CGEventCreateMouseEvent = mouse
        cg.CGEventCreateKeyboardEvent = key
        cg.CGEventCreateScrollWheelEvent = scroll
        cg.CGEventSetIntegerValueField = field
        cg.CGEventSetFlags = lambda ev, flags: field(ev, "flags", flags)
        cg.CGEventCreate = lambda _a: "cursor"
        cg.CGEventGetLocation = lambda _e: _Pt(*rec.cursor)
        cg.CGEventPost = lambda _tap, ev: rec.ev.append(ev)
        return self

    def seq(self):
        out = []
        for ev in self.ev:
            if ev[0] == "mouse":
                t = MOUSE_KIND.get(ev[1], "kind%d" % ev[1])
                for f, v in sorted(ev[3].items()):
                    name = FIELD.get(f)
                    if name:
                        t += " %s=%d" % (name, v)
                out.append(t)
            elif ev[0] == "key":
                t = "key%d" % ev[1]
                if ev[3].get("flags"):
                    t += "+cmd"
                out.append(t if ev[2] else t + "-up")
            else:
                out.append("scroll%+d" % ev[1])
        return out

    def clear(self):
        self.ev = []


def new_bridge(binds=None, hold_ms=250, rc_hold_ms=800, drag_pen=False):
    """建一台 Bridge 并接上录音机 (屏幕按 1000x1000 算, 便于把归一化坐标当像素看)。"""
    rec = Recorder().install()
    with contextlib.redirect_stdout(io.StringIO()):
        b = HB.Bridge(takeover=False, drag_pen=drag_pen, binds=binds,
                      hold_ms=hold_ms, rc_hold_ms=rc_hold_ms)
    b.w = b.h = 1000.0
    b.ox = b.oy = 0.0
    b.say = lambda m: None              # 日志别刷屏, 要看的只有事件
    return b, rec


class Stroke(object):
    """走一笔: 按下 -> 若干次移动 -> 抬手。时间靠回拨 hold_t0 伪造。"""

    def __init__(self, binds=None, hold_ms=250, rc_hold_ms=800, drag_pen=False):
        self.b, self.rec = new_bridge(binds, hold_ms, rc_hold_ms, drag_pen)

    def down(self, x=0.5, y=0.5):
        self.b.x, self.b.y = x, y
        self.b.press()
        return self

    def held(self, seconds):
        """假装已经按住这么久了 (不真等)。"""
        self.b.hold_t0 -= seconds
        for t, d in list(self.b.dev_hist):     # 采样点也要"够老", 否则中位数取不到
            pass
        return self

    def move(self, x=0.5, y=0.5, wait=0.0):
        self.b.x, self.b.y = x, y
        self.b.on_move()
        return self

    def up(self):
        self.b.release()
        return self

    def seq(self):
        return self.rec.seq()


# ------------------------------------------------------------------ 断言框架

FAILED = []
PASSED = [0]


def check(name, got, want):
    ok = got == want
    if ok:
        PASSED[0] += 1
    else:
        FAILED.append((name, want, got))
    print("  %s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("      期望: %s\n      实际: %s" % (want, got))


def check_true(name, cond, detail=""):
    if cond:
        PASSED[0] += 1
        print("  ✓ %s" % name)
    else:
        FAILED.append((name, True, detail))
        print("  ✗ %s\n      实际: %s" % (name, detail))


def section(title):
    print("\n=== %s ===" % title)


# ------------------------------------------------------------------ 默认绑定

def test_defaults():
    section("默认绑定 = 一直以来的行为 (轻点=左键 / 快速滑=滚动 / 停住再滑=拖拽 / 停住不动=右键)")
    check("DEFAULT_BINDS", HB.DEFAULT_BINDS,
          {"tap": "left", "swipe": "scroll", "hold_swipe": "drag", "hold": "right"})

    s = Stroke().down(0.5, 0.5).up()
    check("轻点 -> 一次左键单击", s.seq(), ["LDOWN click=1", "LUP click=1"])

    s = Stroke().down(0.5, 0.5).move(0.5, 0.56).move(0.5, 0.62).up()
    seq = s.seq()
    check_true("快速滑动 -> 有滚轮事件", any(e.startswith("scroll") for e in seq), seq)
    check_true("快速滑动 -> 一个鼠标按键都不发 (不可能被当成划选)",
               not any(e.startswith(("LDOWN", "LUP", "LDRAGGED", "RDOWN", "MUP")) for e in seq), seq)

    s = Stroke().down(0.5, 0.5).held(0.3).move(0.503, 0.5).move(0.55, 0.5).up()
    check("停住再滑 -> 按住左键拖 (LDOWN + LDRAGGED, 抬手 LUP)", s.seq()[:2], ["LDOWN", "LDRAGGED"])
    check("停住再滑 -> 抬手是 LUP, 全程没有滚轮",
          [e for e in s.seq() if e in ("LUP", "LDRAGGED")], ["LDRAGGED", "LDRAGGED", "LUP"])

    s = Stroke().down(0.5, 0.5).held(1.0).up()
    check("停住不动 -> 右键菜单", [e for e in s.seq() if e.startswith(("RDOWN", "RUP"))], ["RDOWN", "RUP"])

    s = Stroke().down(0.5, 0.5).held(0.3).up()
    check("停住 0.3s 抬手 (不到 0.8s) -> 还是轻点", s.seq(), ["LDOWN click=1", "LUP click=1"])


def test_bound_actions():
    section("把动作绑到别的手势上: 每个选项都必须真的有实现")
    cases = [
        ("轻点=右键菜单", {"tap": "right"}, lambda: Stroke({"tap": "right"}).down().up(),
         ["RDOWN", "RUP"]),
        ("轻点=双击", {"tap": "double"}, lambda: Stroke({"tap": "double"}).down().up(),
         ["LDOWN click=1", "LUP click=1", "LDOWN click=2", "LUP click=2"]),
        ("轻点=中键", {"tap": "middle"}, lambda: Stroke({"tap": "middle"}).down().up(),
         ["MDOWN btn=2", "MUP btn=2"]),
        ("轻点=空格键", {"tap": "space"}, lambda: Stroke({"tap": "space"}).down().up(),
         ["key49", "key49-up"]),
        ("轻点=返回", {"tap": "back"}, lambda: Stroke({"tap": "back"}).down().up(),
         ["key33+cmd", "key33+cmd-up"]),
        ("轻点=关", {"tap": "none"}, lambda: Stroke({"tap": "none"}).down().up(), []),
    ]
    for name, binds, run, want in cases:
        check(name, run().seq(), want)

    s = Stroke({"swipe": "drag"}).down(0.5, 0.5).move(0.5, 0.6).up()
    check("快速滑动=拖拽 (老的「滑动选择」= 这个)",
          [e for e in s.seq() if e != "LDRAGGED"], ["LDOWN", "LUP"])

    s = Stroke({"swipe": "right"}).down(0.5, 0.5).move(0.5, 0.6).move(0.5, 0.7).up()
    check("快速滑动=右键菜单 -> 只发一次, 后面的移动不再重复", s.seq(), ["RDOWN", "RUP"])

    s = Stroke({"swipe": "space"}).down(0.5, 0.5).move(0.5, 0.6).up()
    check("快速滑动=空格键", s.seq(), ["key49", "key49-up"])

    s = Stroke({"swipe": "none"}).down(0.5, 0.5).move(0.5, 0.6).move(0.5, 0.7).up()
    check("快速滑动=关 -> 什么都不发", s.seq(), [])

    s = Stroke({"hold_swipe": "none"}).down(0.5, 0.5).held(0.4).move(0.5, 0.62).move(0.5, 0.68).up()
    seq = s.seq()
    check_true("停住再滑=关 -> 那一笔退回滚动, 不会拖拽",
               any(e.startswith("scroll") for e in seq)
               and not any(e.startswith("LDOWN") for e in seq), seq)

    s = Stroke({"hold": "none"}).down(0.5, 0.5).held(1.0).up()
    check("停住不动=关 -> 抬手算轻点", s.seq(), ["LDOWN click=1", "LUP click=1"])

    s = Stroke({"hold": "double"}).down(0.5, 0.5).held(1.0).up()
    check("停住不动=双击", s.seq(),
          ["LDOWN click=1", "LUP click=1", "LDOWN click=2", "LUP click=2"])


def test_screen_action():
    section("「切换屏幕」= 一个事件都不发的动作 (只跟 App 说一声, 由 App 换基准屏)")
    check("动作表里有它", HB.BIND_TITLES.get("screen"), "切换屏幕")
    for g, title, _o, _d in HB.BIND_GESTURES:
        check_true("%s 能绑「切换屏幕」" % title, "screen" in HB.gesture_actions(g),
                   HB.gesture_actions(g))
        check_true("%s 的下拉里有这一项" % title, "切换屏幕" in HB.gesture_action_titles(g),
                   HB.gesture_action_titles(g))

    # 用户点名的用法: 拿它替掉「停住不动 = 右键菜单」。长按抬手只喊一声, 屏幕上什么都不留。
    calls = []
    s = Stroke({"hold": "screen"})
    s.b.on_switch_screen = lambda: calls.append("hold") or True
    s.down(0.5, 0.5).held(1.0).up()
    check("停住不动=切换屏幕 -> 喊一次", calls, ["hold"])
    check("停住不动=切换屏幕 -> 不发任何事件 (没点 / 没右键 / 没滚)", s.seq(), [])

    # 绑在「快速滑动」上时别一边切屏一边滚动 —— 一次性动作发完就完了
    s = Stroke({"swipe": "screen"})
    s.b.on_switch_screen = lambda: calls.append("swipe") or True
    s.down(0.5, 0.5).move(0.5, 0.6).move(0.5, 0.7).up()
    check("快速滑动=切换屏幕 -> 喊一次, 不滚", (calls[-1], s.seq()), ("swipe", []))

    # 命令行单独跑桥接没有引擎: 只记一句日志, 绝不抛
    s = Stroke({"tap": "screen"})
    s.down(0.5, 0.5).up()
    check("没接引擎 -> 静默忽略, 还是不发事件", s.seq(), [])

    # 切屏内部炸了 (Quartz 抽风 / 屏刚拔掉) 也不能把这一笔带崩
    def boom():
        raise RuntimeError("Quartz 抽风")

    s = Stroke({"hold": "screen"})
    s.b.on_switch_screen = boom
    s.down(0.5, 0.5).held(1.0).up()
    check("切屏内部出错 -> 这一笔照常收尾, 不抛不炸", s.seq(), [])

    # 切完屏这笔笔要接着能用 (状态没被打乱)
    s = Stroke({"hold": "screen"})
    s.b.on_switch_screen = lambda: True
    s.down(0.5, 0.5).held(1.0).up()
    s.rec.clear()
    s.down(0.5, 0.5).up()
    check("切屏之后再轻点一下, 照旧一次左键单击", s.seq(), ["LDOWN click=1", "LUP click=1"])


def test_edge_cases():
    section("边界: 手势之间不许互相打架")
    s = Stroke().down(0.5, 0.5).move(0.5, 0.56).move(0.5, 0.62).up()
    check_true("快速划走的那一笔不会再补一次轻点点击",
               not any(e.startswith("RDOWN") for e in s.seq()), s.seq())

    s = Stroke().down(0.5, 0.5).move(0.5, 0.503).move(0.5, 0.506).up()
    seq = s.seq()
    check_true("抖动 (<12px) 不算滑动, 抬手仍是轻点", seq == ["LDOWN click=1", "LUP click=1"], seq)

    s = Stroke().down(0.5, 0.5).move(0.5, 0.53).move(0.5, 0.56).up()
    seq = s.seq()
    check_true("移动 30px 但没到 0.25s -> 滚动, 不是拖拽",
               any(e.startswith("scroll") for e in seq)
               and not any(e.startswith("LDOWN") for e in seq), seq)

    s = Stroke({"hold_swipe": "drag"}).down(0.5, 0.5).held(0.9).move(0.5, 0.52).up()
    seq = s.seq()
    check_true("停住 0.9s 后再滑 -> 拖拽 (不会因为停太久就变成右键)",
               seq[:1] == ["LDOWN"] and "LDRAGGED" in seq and "RUP" not in seq, seq)

    b, rec = new_bridge({"hold": "none"})
    rec.clear()
    b.reset_state()
    check_true("reset_state 把本笔状态清干净",
               (b.down, b.scrolled, b.dragging, b.consumed) == (False, False, False, False))


def test_migration():
    section("老配置迁移 + 恢复默认 (升级后手感不变)")
    check("滑动翻页 (默认) -> 默认绑定", HB.binds_from_cfg(
        {"mode": "scroll", "hold_drag": True, "rc_hold_ms": 800}), HB.DEFAULT_BINDS)
    check("滑动选择 -> 快速滑动=拖拽, 另两个手势关掉", HB.binds_from_cfg({"mode": "select"}),
          {"tap": "left", "swipe": "drag", "hold_swipe": "none", "hold": "none"})
    check("停住再划没勾 -> 停住再滑=关", HB.binds_from_cfg(
        {"mode": "scroll", "hold_drag": False, "rc_hold_ms": 800})["hold_swipe"], "none")
    check("长按不动选「关」-> 停住不动=关", HB.binds_from_cfg(
        {"mode": "scroll", "hold_drag": True, "rc_hold_ms": 0})["hold"], "none")
    check("已有 bind_* 的配置照读不误", HB.binds_from_cfg(
        {"bind_tap": "double", "bind_swipe": "drag", "bind_hold_swipe": "none",
         "bind_hold": "space"}),
        {"tap": "double", "swipe": "drag", "hold_swipe": "none", "hold": "space"})
    check("非法动作值 -> 退回默认", HB.binds_from_cfg({"bind_tap": "拍一下"})["tap"], "left")
    # 更早版本留下的死键 (bind_barrel / bind_eraser) 不算"已经迁移过"
    check("带 bind_barrel 死键的老配置仍会迁移",
          HB.binds_from_cfg({"mode": "select", "bind_barrel": "right"})["swipe"], "drag")
    # 「恢复默认」走的表达式: {bind_<g>: 默认值} -> 读回来必须还是默认
    check("恢复默认 = 写回 DEFAULT_BINDS",
          HB.binds_from_cfg({HB.bind_key(g): a for g, a in HB.DEFAULT_BINDS.items()}),
          dict(HB.DEFAULT_BINDS))


def test_table_consistency():
    section("表的一致性: 界面里出现的动作一定有实现")
    impl = {"none", "left", "right", "middle", "double", "scroll", "drag", "space", "back",
            "screen"}
    titles = [t for _k, t in HB.BIND_ACTIONS]
    check("动作表没有重名", len(titles), len(set(titles)))
    check("动作表 = 已实现的那几个", set(k for k, _t in HB.BIND_ACTIONS), impl)
    for g, title, opts, default in HB.BIND_GESTURES:
        check_true("%s 的默认动作在可绑列表里" % title, default in opts, opts)
        check_true("%s 的选项都在动作表里" % title, set(opts) <= impl, opts)
    check("四个手势的配置键", HB.bind_cfg_keys(),
          ["bind_tap", "bind_swipe", "bind_hold_swipe", "bind_hold"])


def main():
    print("按键绑定离线自检 (不发真事件, 只看拦下来的序列)")
    test_defaults()
    test_bound_actions()
    test_screen_action()
    test_edge_cases()
    test_migration()
    test_table_consistency()
    print("\n" + "-" * 64)
    if FAILED:
        print("失败 %d 项 / 通过 %d 项" % (len(FAILED), PASSED[0]))
        for name, want, got in FAILED:
            print("  ✗ %s\n     期望 %s\n     实际 %s" % (name, want, got))
        return 1
    print("全部通过 (%d 项断言)" % PASSED[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
