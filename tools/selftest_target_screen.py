#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""光标基准屏离线自检 —— 多屏下「笔能不能到别的屏」的判据全在这里。

为什么必须有这个: 笔报的是**归一化绝对坐标** (x,y ∈ [0,1]), 乘进哪块屏的矩形, 光标就
只能在**那块屏**里动。所以「接了第二块屏笔就跨不过去」不是 macOS 的限制, 而是基准屏被
写死在一块屏上。这一层 (选哪块屏 / 指定屏掉线了怎么办 / 认不出平板屏怎么办) 必须能在
真机之外钉住, 否则只能靠开发机插两块屏手试。

真机之外能测的是**判据**; 真机才能测的是**手感** (笔推着光标走的跟手程度)。

用法: python3 tools/selftest_target_screen.py     (全过 exit 0, 有失败 exit 1)
"""

import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import dptouch_display as DP       # noqa: E402
import dptouch_engine as EN        # noqa: E402

PASSED = [0]
FAILED = []


def section(title):
    print("\n== %s" % title)


def check(name, got, want):
    PASSED[0] += 1
    if got == want:
        print("  ✓ %s" % name)
    else:
        FAILED.append((name, want, got))
        print("  ✗ %s\n     期望 %r\n     实际 %r" % (name, want, got))


def check_true(name, ok, detail=""):
    check(name, bool(ok), True)
    if not ok and detail:
        print("     (context: %r)" % (detail,))


# --------------------------------------------------------------------------
# 假显示器: 主屏 2560x1440 在 (0,0); 平板 1704x1136 在主屏右边 (2560,0)
# --------------------------------------------------------------------------

MAIN = {"id": 1, "name": "P27QBC-RG", "main": True, "builtin": False,
        "bounds": (0, 0, 2560, 1440), "match": ""}
TAB = {"id": 2, "name": "MI DISPLAY", "main": False, "builtin": False,
       "bounds": (2560, 0, 1704, 1136), "match": "known"}
MAYBE = {"id": 3, "name": "别的屏", "main": False, "builtin": False,
         "bounds": (2560, 1136, 1920, 1080), "match": "likely"}
# 笔记本那块自带屏 (摆在最左边)。它只是**光标可以铺过去**的目标, 从来不是改档位的对象。
BUILTIN = {"id": 4, "name": "内建视网膜显示器", "main": False, "builtin": True,
           "bounds": (-1704, 0, 1704, 1117), "match": ""}
FULL = [MAIN, TAB]
THREE = [MAIN, TAB, BUILTIN]


class FakeBridge(object):
    """只记 set_rect / takeover 的下发结果。"""

    def __init__(self):
        self.ox = self.oy = 0.0
        self.w = self.h = 1000.0
        self.takeover = False
        self.rect_calls = []

    def set_rect(self, ox, oy, w, h, label=""):
        self.ox, self.oy, self.w, self.h = float(ox), float(oy), float(w), float(h)
        self.rect_calls.append((float(ox), float(oy), float(w), float(h), label))


class EngineStub(object):
    """借 Engine 的几个纯逻辑方法 (不建真 Bridge、不碰 HID)。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.b = FakeBridge()
        self.target = {}
        self.logs = []
        self._lk = threading.RLock()
        self.pending_target = None

    def log(self, m):
        self.logs.append(m)

    target_changed = EN.Engine.target_changed
    _sync_target = EN.Engine._sync_target
    next_screen = EN.Engine.next_screen
    take_pending_target = EN.Engine.take_pending_target


def test_resolve():
    section("基准屏判据: auto / tablet / 指定 displayID")
    r = DP.resolve_target(DP.TARGET_AUTO, FULL, cursor=(100, 100))
    check("跟随光标: 不接管坐标", r["takeover"], False)
    check("跟随光标: 标签", r["label"], "跟随光标")
    check("跟随光标: rect 取光标那块屏", r["rect"], (0, 0, 2560, 1440))

    r = DP.resolve_target(DP.TARGET_AUTO, FULL, cursor=(3000, 500))
    check("跟随光标: 光标在平板上时 rect 跟着变", r["rect"], (2560, 0, 1704, 1136))
    check("跟随光标: 仍然不接管", r["takeover"], False)

    r = DP.resolve_target(DP.TARGET_AUTO, FULL, cursor=(-900, -900))
    check("跟随光标: 光标在屏外(缝隙)时退回主屏", r["rect"], (0, 0, 2560, 1440))

    r = DP.resolve_target(DP.TARGET_TABLET, FULL)
    check("平板那块屏: 接管坐标", r["takeover"], True)
    check("平板那块屏: 认到的是平板", r["id"], 2)
    check("平板那块屏: rect = 平板矩形", r["rect"], (2560, 0, 1704, 1136))
    check("平板那块屏: 标签是屏名", r["label"], "MI DISPLAY")

    r = DP.resolve_target("1", FULL)
    check("指定 displayID: 接管坐标", r["takeover"], True)
    check("指定 displayID: rect = 那块屏的", r["rect"], (0, 0, 2560, 1440))
    check("指定 displayID: kind", r["kind"], "display")


def test_fallbacks():
    section("退回: 认不出 / 掉线 —— 绝不猜, 绝不写死")
    r = DP.resolve_target(DP.TARGET_TABLET, [MAIN, MAYBE], allow_unknown=False)
    check("认不出平板屏 -> 退回跟随光标", r["takeover"], False)
    check("认不出平板屏 -> value 归一成 auto", r["value"], DP.TARGET_AUTO)
    check_true("认不出平板屏 -> 日志里说明原因", "认不出" in r["note"], r["note"])

    r = DP.resolve_target(DP.TARGET_TABLET, [MAIN, MAYBE], allow_unknown=True)
    check("开了「允许未实测显示器」-> 认「疑似」那块", r["id"], 3)
    check("开了之后接管坐标", r["takeover"], True)

    r = DP.resolve_target("9", FULL)
    check("指定的屏不在线 -> 退回跟随光标", r["takeover"], False)
    check_true("指定的屏不在线 -> 日志里说明原因", "不在线" in r["note"], r["note"])

    r = DP.resolve_target("", FULL, cursor=(3000, 300))
    check("空值 = 跟随光标", r["value"], DP.TARGET_AUTO)


def test_choices():
    section("菜单/下拉的条目 (值与配置同构)")
    opts = DP.target_choices(FULL)
    vals = [v for v, _t in opts]
    check("第一项是跟随光标", opts[0], (DP.TARGET_AUTO, "跟随光标"))
    check_true("有「平板」这一项", (DP.TARGET_TABLET, "平板 · MI DISPLAY") in opts, opts)
    check_true("有主屏那一项 (带分辨率)", ("1", "P27QBC-RG · 2560×1440") in opts, opts)
    check("平板那块屏不重复列出", vals, [DP.TARGET_AUTO, DP.TARGET_TABLET, "1"])
    check("值不重名", len(vals), len(set(vals)))

    opts = DP.target_choices([MAIN, MAYBE], allow_unknown=False)
    check("认不出平板屏时不出现「平板」项",
          [v for v, _t in opts], [DP.TARGET_AUTO, "1", "3"])

    opts = DP.target_choices([MAIN], allow_unknown=False)
    check("只有一块屏时也能选它", [v for v, _t in opts], [DP.TARGET_AUTO, "1"])
    check_true("自带屏会标出来",
               "(自带屏)" in DP.target_choices(
                   [{"id": 4, "name": "内置", "main": True, "builtin": True,
                     "bounds": (0, 0, 1704, 1136), "match": ""}])[1][1])


def test_engine_dispatch():
    section("下发到 Bridge: takeover + 矩形 (多屏铁律)")
    st = EngineStub({"target_display": DP.TARGET_AUTO, "display_allow_unknown": False})
    st._sync_target(FULL)
    check("跟随光标: 不接管", st.b.takeover, False)
    check("跟随光标: 也记一笔 rect (日志/调试用)", len(st.b.rect_calls), 1)
    check_true("跟随光标: 日志不吹牛说接管", "接管坐标" not in " ".join(st.logs), st.logs)

    st = EngineStub({"target_display": DP.TARGET_TABLET, "display_allow_unknown": False})
    st._sync_target(FULL)
    check("平板: 接管", st.b.takeover, True)
    check("平板: 矩形铺到平板", st.b.rect_calls[-1][:4], (2560.0, 0.0, 1704.0, 1136.0))
    check("平板: 标签进 rect", st.b.rect_calls[-1][4], "MI DISPLAY")
    n = len(st.logs)
    st._sync_target(FULL)
    check("同一块屏不重复刷日志", len(st.logs), n)

    st = EngineStub({"target_display": "9", "display_allow_unknown": False})
    st._sync_target(FULL)
    check("指定的屏掉线 -> 不接管", st.b.takeover, False)
    check_true("指定的屏掉线 -> note 进日志", any("不在线" in m for m in st.logs), st.logs)


def test_cycle():
    section("「切换屏幕」绑定动作: 按顺序遍历 + 到头绕回 + 只有一块屏不动作")
    check("顺序 = 桌面坐标从左到右 (不看 CGGetActiveDisplayList 的返回顺序)",
          [d["id"] for d in DP.order_displays([MAYBE, TAB, MAIN])], [1, 2, 3])
    check("同一列的两块屏按上下排", [d["id"] for d in DP.order_displays([TAB, MAYBE])], [2, 3])
    check("自带屏也在顺序里 (光标能铺过去它; 它不是改档位的对象)",
          [d["id"] for d in DP.order_displays(THREE)], [4, 1, 2])

    # 平板那块屏在配置里叫 "tablet" (target_choices 的口径): 遍历吐出来的值必须同构,
    # 否则菜单里根本没有那一条 -> 切完勾不动 (上一版就是这么坏的)。
    check("平板在配置里叫 tablet, 不写死 displayID",
          DP.target_value_of([d for d in FULL if d["id"] == 2][0], FULL), "tablet")
    check("主屏那块才写 displayID",
          DP.target_value_of([d for d in FULL if d["id"] == 1][0], FULL), "1")
    check("跟随光标时: 光标在主屏 -> 切到平板", DP.next_target(DP.TARGET_AUTO, FULL, cursor=(100, 100))[0], "tablet")
    check("跟随光标时: 光标在平板 -> 绕回主屏", DP.next_target(DP.TARGET_AUTO, FULL, cursor=(3000, 500))[0], "1")
    check("指定主屏 -> 下一块", DP.next_target("1", FULL)[0], "tablet")
    check("指定平板 (tablet 写法) -> 绕回第一块", DP.next_target(DP.TARGET_TABLET, FULL)[0], "1")
    check("指定平板 (老 displayID 写法) -> 一样认", DP.next_target("2", FULL)[0], "1")
    check("三块屏: 最右那块 -> 绕回最左那块 (自带屏)", DP.next_target("2", THREE)[0], "4")
    # 指定的屏掉线 -> 从顺序里的第一块 (最左那块) 起头, 不卡死。断言写成"等于顺序里
    # 的第一块"而不是写死某个值: 免得以后改夹具时这条又假失败。
    _r9, _why9 = DP.next_target("9", FULL)
    check("指定的屏已掉线 -> 从第一块起头, 不卡死", _r9,
          DP.target_value_of(DP.order_displays(FULL)[0], FULL))
    check_true("掉线那条路吐的值也在菜单里 (不会点了勾不动)",
               _r9 in [v for v, _t in DP.target_choices(FULL)], str(_r9))

    _vals = [c[0] for c in DP.target_choices(FULL)]
    _walk, _v = [], DP.TARGET_AUTO
    for _i in range(4):
        _v, _w = DP.next_target(_v, FULL, cursor=(100, 100))
        _walk.append(_v)
    check("遍历吐的值菜单里真有条目 (这条断了就是「点了勾不动」)",
          [w in _vals for w in _walk], [True] * 4)
    check("连按四下 -> 两块屏来回转", _walk, ["tablet", "1", "tablet", "1"])
    check("只有一块屏 -> 不切 (给原因)",
          DP.next_target(DP.TARGET_AUTO, [MAIN]), (None, "只有 1 块屏, 不用切"))
    check("只有一块屏可切时不会指到屏幕外",
          DP.next_target("1", [MAIN])[0], None)

    old_displays, old_cursor = DP.displays, DP.cursor_point
    try:
        DP.displays = lambda: FULL
        DP.cursor_point = lambda: (100, 100)
        st = EngineStub({"target_display": DP.TARGET_AUTO, "display_allow_unknown": False})
        st._sync_target(FULL)
        check("切之前: 跟随光标 = 不接管", st.b.takeover, False)

        check("切一次 -> 值 = 右边那块", st.next_screen(), "tablet")
        check("切一次 -> 配置也跟着改 (笔上切的也能存住)", st.cfg["target_display"], "tablet")
        check("切一次 -> 接管坐标 (笔才推得动光标)", st.b.takeover, True)
        check("切一次 -> 矩形铺到那块屏", st.b.rect_calls[-1][:4], (2560.0, 0.0, 1704.0, 1136.0))
        check("切一次 -> 留了待办给主线程 (菜单的勾靠它跟上)", st.take_pending_target(), "tablet")
        check("待办取过即清 (不会一直重复刷菜单)", st.take_pending_target(), None)
        check("再切一次 -> 绕回主屏", (st.next_screen(), st.b.rect_calls[-1][:4]),
              ("1", (0.0, 0.0, 2560.0, 1440.0)))
        check("第三下 -> 又回平板 (两块屏 = 来回转)", st.next_screen(), "tablet")

        # 只有一块屏: 什么都不做 —— 宁可不响应, 也不把笔指到不存在的地方
        DP.displays = lambda: [MAIN]
        st2 = EngineStub({"target_display": DP.TARGET_AUTO, "display_allow_unknown": False})
        st2._sync_target([MAIN])
        n0 = len(st2.b.rect_calls)
        check("只有一块屏 -> 不切, 返回 None", (st2.next_screen(), st2.b.takeover), (None, False))
        check("只有一块屏 -> 一个矩形都没重下", len(st2.b.rect_calls), n0)
        check_true("只有一块屏 -> 日志里说明原因",
                   any("只有 1 块屏" in m for m in st2.logs), st2.logs)

        # 硬约束: 切屏只换基准矩形, 绝不碰别的屏的档位/排列 —— 把那些函数换成炸弹来证
        real_switch, real_apply = DP.switch, DP.apply

        def boom(*_a, **_k):
            raise AssertionError("「切换屏幕」动到了显示档位!")

        DP.switch = DP.apply = boom
        try:
            DP.displays = lambda: THREE
            st3 = EngineStub({"target_display": "1", "display_allow_unknown": False})
            check("连切三下 -> 主屏/平板/自带屏 转一圈",
                  [st3.next_screen() for _ in range(3)], ["tablet", "4", "1"])
            check("全程只动过基准矩形",
                  [c[:4] for c in st3.b.rect_calls],
                  [(2560.0, 0.0, 1704.0, 1136.0), (-1704.0, 0.0, 1704.0, 1117.0),
                   (0.0, 0.0, 2560.0, 1440.0)])
        finally:
            DP.switch, DP.apply = real_switch, real_apply
    finally:
        DP.displays, DP.cursor_point = old_displays, old_cursor


def test_cfg_migration():
    section("配置层: 老 takeover -> 光标基准屏, 非法值兜底")
    import dptouch as APP

    tmpd = tempfile.mkdtemp(prefix="dptouch-cfg-")
    old_path, old_dir = APP.CFG_PATH, APP.CFG_DIR
    APP.CFG_PATH = os.path.join(tmpd, "config.json")
    APP.CFG_DIR = tmpd
    try:
        def load(raw):
            with open(APP.CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            return APP.load_cfg()

        check("老配置 takeover=true -> 平板那块屏",
              load({"takeover": True})["target_display"], DP.TARGET_TABLET)
        check("老配置 takeover=false -> 跟随光标",
              load({"takeover": False})["target_display"], DP.TARGET_AUTO)
        check("没有 takeover 的老配置 -> 跟随光标 (老手感不变)",
              load({"mode": "scroll"})["target_display"], DP.TARGET_AUTO)
        check("已经写过的值不被覆盖",
              load({"takeover": True, "target_display": "1"})["target_display"], "1")
        check("非法值 -> 跟随光标", load({"target_display": "左边那块"})["target_display"],
              DP.TARGET_AUTO)
        check("空值 -> 跟随光标", load({"target_display": "  "})["target_display"],
              DP.TARGET_AUTO)
        check("数字字符串保留", load({"target_display": "7"})["target_display"], "7")
        check("目标屏不出现在老配置迁移的判据里 (四类手势键才是)",
              load({"takeover": True})["bind_tap"], "left")
    finally:
        APP.CFG_PATH, APP.CFG_DIR = old_path, old_dir


def main():
    print("光标基准屏离线自检 (只验判据, 不碰真机)")
    test_resolve()
    test_fallbacks()
    test_choices()
    test_engine_dispatch()
    test_cycle()
    test_cfg_migration()
    print("\n" + "-" * 64)
    if FAILED:
        print("失败 %d 项 / 通过 %d 项" % (len(FAILED), PASSED[0]))
        return 1
    print("全部通过 (%d 项断言)" % PASSED[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
