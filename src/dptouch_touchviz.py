#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触控可视化 —— 系统级实时覆盖层。

每块屏铺一个透明、点击穿透的覆盖窗，触点按 App 实际使用的映射
（基准屏矩形 ox/oy/w/h）画在**屏幕上真实的位置**：位置修正生效后看到的就是修正后的落点。
覆盖层关掉即停，不额外开 HID 设备、不合成事件。
"""

import os
import time
from collections import deque

import objc

from AppKit import (
    NSBackingStoreBuffered, NSBezierPath, NSColor, NSMakeRect, NSScreen,
    NSApplication, NSScreenSaverWindowLevel, NSPanel, NSTimer, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
)

TRAIL_SEC = 1.0          # 轨迹保留时长
STALE_SEC = 0.35          # 抬手后点还留多久
FPS = 60.0

FINGER = (1.00, 0.45, 0.10)
PEN = (0.35, 0.65, 1.00)


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x1 = min(a.origin.x, b.origin.x)
    y1 = min(a.origin.y, b.origin.y)
    x2 = max(a.origin.x + a.size.width, b.origin.x + b.size.width)
    y2 = max(a.origin.y + a.size.height, b.origin.y + b.size.height)
    return NSMakeRect(x1, y1, x2 - x1, y2 - y1)


def _dot_rect(pt, is_pen):
    pad = 34.0 if is_pen else 26.0
    return NSMakeRect(pt[0] - pad, pt[1] - pad, pad * 2, pad * 2)


class OverlayView(NSView):
    """一块屏一个；翻转为左上原点。"""

    def initWithFrame_(self, rect):
        self = objc.super(OverlayView, self).initWithFrame_(rect)
        if self is None:
            return None
        self.overlay = None
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, dirty):
        if self.overlay is not None:
            self.overlay.draw_in_view(self)


class TouchOverlay:
    """系统级触控覆盖层。show()/hide()/toggle()，刷新交给 60fps 定时器。"""

    def __init__(self, engine, log=None):
        self.engine = engine
        self.log = log or (lambda *_: None)
        self.is_on = False
        self._wins = []                 # [(NSScreen, NSPanel, OverlayView)]
        self._trails = {}               # key -> deque[(gx, gy, t)]
        self._points = []
        self._boxes = {}                # id(view) -> 上一帧画过的区域
        self._hidden_own = False        # 本应用前台时整组收起
        self._free_since = None         # 连续「不是本应用前台」的起点（防闪）
        self._timer = None

    # ---------------- 生命周期 ----------------
    def show(self):
        self.is_on = True
        self.engine.set_live(True)
        self._build_windows()
        self._hidden_own = self.own_front()      # 本应用前台：先不上屏，切走再显示
        if not self._hidden_own:
            for _s, w, _v in self._wins:
                w.orderFrontRegardless()
        if self._timer is None or not self._timer.isValid():
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / FPS, self, "tick:", None, True)

    def hide(self):
        self.is_on = False
        self.engine.set_live(False)
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None
        for _s, w, _v in self._wins:
            w.orderOut_(None)
        self._wins = []
        self._trails = {}
        self._points = []
        self._boxes = {}
        self._hidden_own = False

    def toggle(self):
        self.hide() if self.is_on else self.show()
        return self.is_on

    def _build_windows(self):
        self._wins = []
        for scr in NSScreen.screens():
            f = scr.frame()
            w = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, f.size.width, f.size.height), 0,
                NSBackingStoreBuffered, False)
            w.setLevel_(NSScreenSaverWindowLevel)
            w.setOpaque_(False)
            w.setBackgroundColor_(NSColor.clearColor())
            w.setHasShadow_(False)
            w.setIgnoresMouseEvents_(True)
            w.setReleasedWhenClosed_(False)
            w.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorIgnoresCycle
                | NSWindowCollectionBehaviorFullScreenAuxiliary)
            w.setFrame_display_(f, False)
            v = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, f.size.width, f.size.height))
            v.overlay = self
            w.setContentView_(v)
            self._wins.append((scr, w, v))

    # ---------------- 坐标 ----------------
    def _main_h(self):
        for s in NSScreen.screens():
            f = s.frame()
            if f.origin.x == 0 and f.origin.y == 0:
                return f.size.height
        m = NSScreen.mainScreen()
        return m.frame().size.height if m is not None else 0.0

    def _to_local(self, scr, gx, gy, main_h):
        """CG 全局坐标（左上原点）-> 该屏窗口内坐标（flipped，左上原点）。"""
        f = scr.frame()
        return (gx - f.origin.x, f.origin.y + f.size.height - main_h + gy)

    # ---------------- 每帧 ----------------
    def own_front(self):
        """本应用是否是当前活跃 App。NSWorkspace.frontmostApplication 在 accessory 应用上会抖，
        用 NSApp.isActive() 更贴近「用户正在操作我们自己的窗口」。"""
        app = NSApplication.sharedApplication()
        return bool(app is not None and app.isActive())

    def tick_(self, _timer):
        # 本应用自己在前台时整组收起：既没有参考价值，也会和主线程的事件处理抢时间。
        # 激活状态会抖（AX 请求、弹窗），所以要求连续 0.3s 都不是本应用前台才上屏。
        if self.own_front():
            self._free_since = None
            if not self._hidden_own:
                for _s, w, _v in self._wins:
                    w.orderOut_(None)
                self._hidden_own = True
                self._points = []
                self._trails = {}
                self.log("触控可视化: 本应用前台，覆盖层收起")
            return
        now = time.time()
        if self._free_since is None:
            self._free_since = now
        if self._hidden_own and now - self._free_since >= 0.3:
            for _s, w, _v in self._wins:
                w.orderFrontRegardless()
            self._hidden_own = False
            self.log("触控可视化: 覆盖层显示")

        snap = self.engine.live_snapshot()
        now = snap["now"]
        rect = snap.get("rect")

        if len(NSScreen.screens()) != len(self._wins):     # 插拔外接屏
            self._build_windows()
            for _s, w, _v in self._wins:
                w.orderFrontRegardless()

        points = []
        if rect:
            ox, oy, rw, rh = rect
            for i, f in enumerate(snap["fingers"]):
                if f["x"] is None or f["y"] is None:
                    continue
                if not f["down"] and now - f["t"] > STALE_SEC:
                    continue
                points.append(("f%d" % i, ox + f["x"] * rw, oy + f["y"] * rh,
                               bool(f["down"]), False, 0.0))
            p = snap["pen"]
            if p["x"] is not None and p["y"] is not None and (p["down"] or now - p["t"] < 1.0):
                points.append(("pen", ox + p["x"] * rw, oy + p["y"] * rh,
                               bool(p["down"]), True, float(p["pres"])))

        alive = {k for k, *_ in points}
        for k in list(self._trails):
            if k not in alive:
                self._trails.pop(k, None)
        for k, gx, gy, _down, _pen, _pres in points:
            tr = self._trails.setdefault(k, deque(maxlen=90))
            if not tr or abs(tr[-1][0] - gx) > 0.5 or abs(tr[-1][1] - gy) > 0.5:
                tr.append((gx, gy, now))
            while tr and now - tr[0][2] > TRAIL_SEC:
                tr.popleft()

        had = bool(self._points) or bool(self._trails)
        self._points = points
        if points or had:                 # 空闲时不重画
            self._redraw()

    # ---------------- 重绘区域 ----------------
    def _redraw(self):
        """只失效有内容的区域。整块 1704×1136 透明窗每帧全量重绘会把主线程拖住。"""
        main_h = self._main_h()
        for scr, _w, v in self._wins:
            box = None
            for _k, gx, gy, _d, is_pen, _p in self._points:
                box = _union(box, _dot_rect(self._to_local(scr, gx, gy, main_h), is_pen))
            for _k, tr in self._trails.items():
                for x1, y1, _t in list(tr):
                    box = _union(box, _dot_rect(self._to_local(scr, x1, y1, main_h), False))
            prev = self._boxes.get(id(v))
            if prev is not None:
                box = _union(box, prev)
            self._boxes[id(v)] = box
            if box is not None:
                v.setNeedsDisplayInRect_(box)

    # ---------------- 绘制 ----------------
    def draw_in_view(self, view):
        scr = None
        for s, _w, v in self._wins:
            if v is view:
                scr = s
                break
        if scr is None:
            return
        main_h = self._main_h()
        now = time.time()

        for key, tr in self._trails.items():
            if len(tr) < 2:
                continue
            is_pen = key == "pen"
            col = PEN if is_pen else FINGER
            items = list(tr)
            for (x1, y1, t1), (x2, y2, t2) in zip(items[:-1], items[1:]):
                age = now - t2
                if age > TRAIL_SEC:
                    continue
                a = max(0.0, 1.0 - age / TRAIL_SEC) * 0.70
                lx1, ly1 = self._to_local(scr, x1, y1, main_h)
                lx2, ly2 = self._to_local(scr, x2, y2, main_h)
                p = NSBezierPath.bezierPath()
                p.moveToPoint_((lx1, ly1))
                p.lineToPoint_((lx2, ly2))
                p.setLineWidth_(5.0 if is_pen else 8.0)
                p.setLineCapStyle_(1)
                NSColor.colorWithCalibratedRed_green_blue_alpha_(col[0], col[1], col[2], a).set()
                p.stroke()

        for key, gx, gy, down, is_pen, pres in self._points:
            lx, ly = self._to_local(scr, gx, gy, main_h)
            f = scr.frame()
            if not (0 <= lx <= f.size.width and 0 <= ly <= f.size.height):
                continue
            col = PEN if is_pen else FINGER
            if is_pen:
                r = 9.0 + 16.0 * max(0.0, min(1.0, pres / 4095.0)) if down else 9.0
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    col[0], col[1], col[2], 0.9 if down else 0.55).set()
                p = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(lx - r, ly - r, r * 2, r * 2))
                p.setLineWidth_(2.5)
                p.stroke()
                p = NSBezierPath.bezierPath()
                p.moveToPoint_((lx - r - 9, ly))
                p.lineToPoint_((lx + r + 9, ly))
                p.moveToPoint_((lx, ly - r - 9))
                p.lineToPoint_((lx, ly + r + 9))
                p.stroke()
            else:
                r = 17.0 if down else 12.0
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    col[0], col[1], col[2], 0.28 if down else 0.16).set()
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(lx - r, ly - r, r * 2, r * 2)).fill()
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    col[0], col[1], col[2], 0.95 if down else 0.55).set()
                p = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(lx - r, ly - r, r * 2, r * 2))
                p.setLineWidth_(2.0)
                p.stroke()
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(lx - 3, ly - 3, 6, 6)).fill()
