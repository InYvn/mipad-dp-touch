#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触控可视化 —— 只读面板。

把平板报上来的触点画在一个 3:2 的方框里：手指是实心圆（按下橙色、悬停灰色），
笔是十字加一个按压力大小的圈。数据来自 Engine.live_snapshot()，面板关掉就停，
不参与任何合成，也不额外开 HID 设备。
"""

import time

import objc

from AppKit import (
    NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSFloatingWindowLevel, NSMakeRect, NSObject,
    NSPanel, NSString, NSTimer, NSView, NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel, NSWindowStyleMaskTitled,
)

PANEL_W, PANEL_H = 460.0, 430.0
PAD = 20.0
TOP = 46.0
BOTTOM = 62.0
ASPECT = 3.0 / 2.0          # 平板 1704×1136


def _text(s, x, y, size=11.0, alpha=0.75, bold=False):
    attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(size) if bold
        else NSFont.systemFontOfSize_(size),
        NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha),
    }
    NSString.alloc().initWithString_(s).drawAtPoint_withAttributes_((x, y), attrs)


def _rect(x, y, w, h, fill=None, stroke=None, radius=0.0, width=1.0):
    p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(x, y, w, h), radius, radius)
    if fill is not None:
        fill.set()
        p.fill()
    if stroke is not None:
        stroke.set()
        p.setLineWidth_(width)
        p.stroke()


class VizView(NSView):
    """翻转为左上原点，坐标直接按归一化位置摆。"""

    def initWithFrame_(self, rect):
        self = objc.super(VizView, self).initWithFrame_(rect)
        if self is None:
            return None
        self.viz = None
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, dirty):
        b = self.bounds()
        NSColor.colorWithCalibratedWhite_alpha_(0.09, 1.0).set()
        NSBezierPath.fillRect_(b)

        snap = self.viz.snapshot() if self.viz is not None else None
        if snap is None:
            _text("引擎未就绪", PAD, TOP, 12.0, 0.5)
            return

        now = snap["now"]
        mode = "原生触控" if snap["native"] else "兼容模式"
        fingers = snap["fingers"]
        live = [(i, f) for i, f in enumerate(fingers)
                if f["x"] is not None and f["y"] is not None
                and (f["down"] or now - f["t"] < 0.35)]
        pen = snap["pen"]
        pen_live = (pen["x"] is not None and pen["y"] is not None
                    and (pen["down"] or now - pen["t"] < 1.2))

        # 标题行
        _text(mode + " · 触点 %d" % len(live), PAD, 16, 12.0, 0.9, bold=True)
        state = "未接入" if not snap["touch_iface"] else ("运行中" if snap["running"] else "已暂停")
        _text("触控接口：%s" % state, PANEL_W - PAD - 130, 16, 11.0, 0.55)

        # 平板区域：按 3:2 放进可用空间
        avail_w = b.size.width - PAD * 2
        avail_h = b.size.height - TOP - BOTTOM
        if avail_w / avail_h > ASPECT:
            ah = avail_h
            aw = ah * ASPECT
        else:
            aw = avail_w
            ah = aw / ASPECT
        ax = PAD + (avail_w - aw) / 2
        ay = TOP + (avail_h - ah) / 2

        _rect(ax, ay, aw, ah, fill=NSColor.colorWithCalibratedWhite_alpha_(0.04, 1.0),
              stroke=NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18), radius=10.0, width=1.0)
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.06).set()
        for k in (1, 2, 3):
            gx = ax + aw * k / 4.0
            gy = ay + ah * k / 4.0
            p = NSBezierPath.bezierPath()
            p.moveToPoint_((gx, ay + 8))
            p.lineToPoint_((gx, ay + ah - 8))
            p.stroke()
            p = NSBezierPath.bezierPath()
            p.moveToPoint_((ax + 8, gy))
            p.lineToPoint_((ax + aw - 8, gy))
            p.stroke()

        # 手指
        for i, f in live:
            cx = ax + f["x"] * aw
            cy = ay + f["y"] * ah
            r = 13.0 if f["down"] else 9.0
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.45, 0.10, 0.85 if f["down"] else 0.45).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2)).fill()
            _text(str(i + 1), cx - 3.5, cy - 7, 10.0, 0.95, bold=True)

        # 笔
        if pen_live:
            cx = ax + pen["x"] * aw
            cy = ay + pen["y"] * ah
            pres = max(0.0, min(1.0, float(pen["pres"]) / 4095.0))
            r = 8.0 + 14.0 * pres if pen["down"] else 8.0
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.35, 0.65, 1.0, 0.9 if pen["down"] else 0.5).set()
            p = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, r * 2, r * 2))
            p.setLineWidth_(2.0)
            p.stroke()
            p = NSBezierPath.bezierPath()
            p.moveToPoint_((cx - r - 7, cy))
            p.lineToPoint_((cx + r + 7, cy))
            p.moveToPoint_((cx, cy - r - 7))
            p.lineToPoint_((cx, cy + r + 7))
            p.stroke()

        # 底部状态
        y = b.size.height - BOTTOM + 12
        if pen_live:
            pen_s = "笔：%s · %d,%d · 压力 %d" % (
                "按下" if pen["down"] else "悬停",
                round(pen["x"] * 100), round(pen["y"] * 100), int(pen["pres"]))
        else:
            pen_s = "笔：未接入"
        _text(pen_s, PAD, y, 11.0, 0.7)
        _text("触摸平板，点会出现在方框里" if not live else "触点 %d 个" % len(live),
              PAD, y + 18, 11.0, 0.5)


class VizWindowDelegate(NSObject):
    """窗口关闭时停掉 30fps 刷新和只读快照。"""

    def initWithViz_(self, viz):
        self = objc.super(VizWindowDelegate, self).init()
        if self is None:
            return None
        self.viz = viz
        return self

    def windowWillClose_(self, _note):
        if self.viz is not None:
            self.viz.on_close()


class TouchViz:
    """面板 + 定时刷新。宿主只需要 show() / close()。"""

    def __init__(self, engine, log=None):
        self.engine = engine
        self.log = log or (lambda *_: None)
        self.win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.win.setTitle_("触控可视化")
        self.win.setLevel_(NSFloatingWindowLevel)
        self.win.setReleasedWhenClosed_(False)
        self._delegate = VizWindowDelegate.alloc().initWithViz_(self)
        self.win.setDelegate_(self._delegate)
        self.view = VizView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        self.view.viz = self
        self.win.setContentView_(self.view)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 30.0, self, "tick:", None, True)
        self._snap = None
        self._last = 0.0

    # 30fps 只重画；快照内部自己加锁
    def tick_(self, _timer):
        self._snap = self.engine.live_snapshot()
        self.view.setNeedsDisplay_(True)

    def snapshot(self):
        if self._snap is None or time.time() - self._last > 0.5:
            self._snap = self.engine.live_snapshot()
            self._last = time.time()
        return self._snap

    def show(self):
        self.engine.set_live(True)
        if not self.timer.isValid():      # 关过一次后 timer 已失效，重开要重建
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 30.0, self, "tick:", None, True)
        if not self.win.isVisible():
            self.win.center()
        self.win.orderFrontRegardless()

    def close(self):
        self.on_close()
        self.win.orderOut_(None)

    def on_close(self):
        self.engine.set_live(False)
        try:
            self.timer.invalidate()
        except Exception:
            pass
