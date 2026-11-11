#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断开平板后锁屏 —— 只锁屏，不解锁，不碰密码。

为什么只做锁屏
--------------
锁屏不需要认证、不涉及密码注入，失败最多是「没锁上」，不会把用户锁在门外。
自动解锁则必须有密码学身份（DP-in 的 EDID/序列号可伪造），不适合做进这里。

判定规则
--------
* 只有「本进程见过平板在线」之后掉线，才开始计时（启动时就没插 = 不锁）。
* 掉线持续 >= delay 秒才触发一次；中途重新连上则取消。
* 触发后不重复；重新连上再掉线会重新计时。
"""

import ctypes

DELAY_DEFAULT = 8.0
_LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"

_login = None


class LockDecider:
    """(now, present) -> 是否要锁屏。纯逻辑，可离线测试。"""

    def __init__(self, delay=DELAY_DEFAULT):
        self.delay = float(delay)
        self.armed = False          # 曾经见过平板在线
        self.absent_since = None
        self.fired = False

    def update(self, now, present):
        if present:
            self.armed = True
            self.absent_since = None
            self.fired = False
            return False
        if not self.armed or self.fired:
            return False
        if self.absent_since is None:
            self.absent_since = now
            return False
        if now - self.absent_since >= self.delay:
            self.fired = True
            return True
        return False

    def reset(self):
        self.armed = False
        self.absent_since = None
        self.fired = False


def external_display_count():
    """当前还接着几块外接屏（不含内置屏）。

    用途：只在「平板是唯一外接屏」时才锁。平板拔掉后如果还剩别的显示器，
    说明人还在用别的屏，不该锁。
    读不出来返回 -1（调用方按“还有别的屏”处理，宁可不锁）。
    """
    try:
        from AppKit import NSScreen
        from Quartz import CGDisplayIsBuiltin
        n = 0
        for s in NSScreen.screens():
            name = str(s.localizedName() or "")
            if "MI DISPLAY" in name:        # 平板自己不算“别的屏”
                continue
            did = s.deviceDescription().objectForKey_("NSScreenNumber")
            if did is not None and not CGDisplayIsBuiltin(int(did)):
                n += 1
        return n
    except Exception:
        return -1


def screen_locked():
    """当前会话是否已经锁屏（已锁就不用再锁）。"""
    try:
        from Quartz import CGSessionCopyCurrentDictionary
        d = CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked"))
    except Exception:
        return False


def lock_screen():
    """调 login.framework 的 SACLockScreenImmediate；成功返回 True。"""
    global _login
    try:
        if _login is None:
            _login = ctypes.CDLL(_LOGIN_FRAMEWORK)
        _login.SACLockScreenImmediate()
        return True
    except Exception:
        return False


def selftest_offline():
    ok, bad = 0, []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    d = LockDecider(delay=5)
    ck("启动时没插不锁", not d.update(0, False))
    ck("仍不锁", not d.update(100, False))
    ck("插上 armed", not d.update(101, True))
    ck("刚掉线不锁", not d.update(102, False))
    ck("未到时间不锁", not d.update(106, False))
    ck("到时间锁", d.update(107, False))
    ck("不重复锁", not d.update(200, False))

    d2 = LockDecider(delay=5)
    d2.update(0, True)
    d2.update(1, False)
    ck("中途重连取消", not d2.update(3, True) and not d2.update(4, False))
    ck("重连后重新计时: 未到时间", not d2.update(8, False))
    ck("重连后重新计时: 到时间", d2.update(9.2, False))

    d3 = LockDecider(delay=0)
    d3.update(0, True)
    ck("delay=0 首拍只起计时", not d3.update(1, False))
    ck("delay=0 第二拍触发", d3.update(1.01, False))
    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest_offline(), ensure_ascii=False))
