#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览显示策略: DP-in 预览时把 MI DISPLAY 设为 1704x1136@120 并作为主显示器。

只做两件事:
  1. 若当前逻辑分辨率不是 1704x1136 HiDPI, 切到该档;
  2. 把 MI DISPLAY 的全局原点移到 (0,0), 其它屏平移同样偏移, 从而成为主显示器。

调用方是 Mipad DP Touch 的每秒 tick, 外部按 native_touch_active 节流;
本模块自身也幂等, 当前状态已满足时返回 changed=False。
"""

import Quartz
import dptouch_display as DP

PREVIEW_W = 1704
PREVIEW_H = 1136
PREVIEW_HZ = 120.0


def _bounds(did):
    r = Quartz.CGDisplayBounds(int(did))
    return (float(r.origin.x), float(r.origin.y),
            float(r.size.width), float(r.size.height))


def _main(did):
    try:
        return int(Quartz.CGMainDisplayID()) == int(did)
    except Exception:
        return False


def _switch_to_preview(did):
    cur = DP.current(did)
    if (cur.get("w"), cur.get("h")) == (PREVIEW_W, PREVIEW_H) and cur.get("hidpi"):
        return False
    mode = DP.find_mode(did, PREVIEW_W, PREVIEW_H, PREVIEW_HZ, True)
    if mode is None:
        mode = DP.find_mode(did, PREVIEW_W, PREVIEW_H, None, True)
    if mode is None:
        mode = DP.find_mode(did, PREVIEW_W, PREVIEW_H, None, None)
    if mode is None:
        raise RuntimeError("MI DISPLAY 没有 1704x1136 档位")
    err = DP.switch(did, mode["ref"])
    if err != 0:
        raise RuntimeError("切换预览分辨率 CGError=%d" % err)
    return True


def _make_main(infos, did):
    """把 target 移到 (0,0); 其它在线屏按同一 dx/dy 平移, 保持相对排列。"""
    if _main(did):
        return False
    tx, ty, _, _ = _bounds(did)
    dx, dy = -tx, -ty
    err, cfg = Quartz.CGBeginDisplayConfiguration(None)
    if err != 0:
        raise RuntimeError("CGBeginDisplayConfiguration err=%d" % err)
    try:
        for info in infos:
            x, y, _, _ = _bounds(info["id"])
            e = Quartz.CGConfigureDisplayOrigin(cfg, int(info["id"]), x + dx, y + dy)
            if e != 0:
                raise RuntimeError("CGConfigureDisplayOrigin(%s) err=%d" % (info["id"], e))
        e = Quartz.CGCompleteDisplayConfiguration(cfg, Quartz.kCGConfigurePermanently)
        if e != 0:
            raise RuntimeError("CGCompleteDisplayConfiguration err=%d" % e)
    except Exception:
        try:
            Quartz.CGCancelDisplayConfiguration(cfg)
        except Exception:
            pass
        raise
    return True


def ensure_preview_display(allow_unverified=False):
    """幂等确保 MI DISPLAY = 1704x1136@120 且为主显示器。"""
    infos = DP.displays()
    target, why = DP.pick_target(allow_unverified, infos)
    if target is None:
        return {"ok": False, "changed": False, "msg": "未识别平板屏: " + str(why), "target": None}
    did = target["id"]
    changed = []
    if _switch_to_preview(did):
        changed.append("mode=%dx%d" % (PREVIEW_W, PREVIEW_H))
    if _make_main(infos, did):
        changed.append("main")
    return {"ok": True, "changed": bool(changed),
            "msg": "预览显示: " + (", ".join(changed) if changed else "已是 1704x1136 主屏"),
            "target": did}
