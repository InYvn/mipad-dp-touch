#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便利解锁：把登录密码存在钥匙串，锁屏时由本进程输入。

它**不是** Touch ID，也不是安全解锁。开启前必须在界面上明确告知：
任何能伪装成这块平板屏（EDID/序列号均可伪造）的 USB-C 设备，都可能触发解锁。

存储
----
* 钥匙串通用密码项：service = ``MipadDPTouch-unlock``，account = 当前用户名
* 通过 ``/usr/bin/security`` 读写；只允许 ``/usr/bin/security`` 访问，避免重打包后弹授权框
* 密码只在内存里短暂存在，从不写日志、不落文件

注入
----
* CGEvent 合成按键（US 布局），只支持可打印 ASCII；非 ASCII 密码在开启时就拒绝
* 锁屏的 Secure Input 可能吞事件 —— 这是系统行为，失败只能如实报错
"""

import os
import subprocess

SERVICE = "MipadDPTouch-unlock"
SECURITY = "/usr/bin/security"
RETURN_KEY = 36

# US 布局：字符 -> (虚拟键码, 是否需要 Shift)
KEYMAP = {
    "a": (0, False), "b": (11, False), "c": (8, False), "d": (2, False),
    "e": (14, False), "f": (3, False), "g": (5, False), "h": (4, False),
    "i": (34, False), "j": (38, False), "k": (40, False), "l": (37, False),
    "m": (46, False), "n": (45, False), "o": (31, False), "p": (35, False),
    "q": (12, False), "r": (15, False), "s": (1, False), "t": (17, False),
    "u": (32, False), "v": (9, False), "w": (13, False), "x": (7, False),
    "y": (16, False), "z": (6, False),
    "0": (29, False), "1": (18, False), "2": (19, False), "3": (20, False),
    "4": (21, False), "5": (23, False), "6": (22, False), "7": (26, False),
    "8": (28, False), "9": (25, False),
    " ": (49, False), "-": (27, False), "=": (24, False), "[": (33, False),
    "]": (30, False), "\\": (42, False), ";": (41, False), "'": (39, False),
    ",": (43, False), ".": (47, False), "/": (44, False), "`": (50, False),
    "A": (0, True), "B": (11, True), "C": (8, True), "D": (2, True),
    "E": (14, True), "F": (3, True), "G": (5, True), "H": (4, True),
    "I": (34, True), "J": (38, True), "K": (40, True), "L": (37, True),
    "M": (46, True), "N": (45, True), "O": (31, True), "P": (35, True),
    "Q": (12, True), "R": (15, True), "S": (1, True), "T": (17, True),
    "U": (32, True), "V": (9, True), "W": (13, True), "X": (7, True),
    "Y": (16, True), "Z": (6, True),
    "!": (18, True), "@": (19, True), "#": (20, True), "$": (21, True),
    "%": (23, True), "^": (22, True), "&": (26, True), "*": (28, True),
    "(": (25, True), ")": (29, True), "_": (27, True), "+": (24, True),
    "{": (33, True), "}": (30, True), "|": (42, True), ":": (41, True),
    '"': (39, True), "<": (43, True), ">": (47, True), "?": (44, True),
    "~": (50, True),
}


def unsupported_chars(password):
    """返回无法用键码注入的字符（非 US 布局可打印 ASCII）。"""
    return sorted({c for c in str(password or "") if c not in KEYMAP})


# ---------------------------------------------------------------- 钥匙串

def _run(args, stdin_text=None):
    try:
        r = subprocess.run([SECURITY] + args, capture_output=True, text=True,
                           input=stdin_text, timeout=10)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return -1, "", repr(e)


def store_password(password, account=None):
    """写入（覆盖）钥匙串；返回 (ok, why)。"""
    acct = account or os.environ.get("USER") or "user"
    if not password:
        return False, "密码为空"
    bad = unsupported_chars(password)
    if bad:
        return False, "密码含无法注入的字符：%s" % "".join(bad)
    # 先删再写，确保 ACL 是我们指定的
    _run(["delete-generic-password", "-s", SERVICE, "-a", acct])
    rc, _out, err = _run(["add-generic-password", "-a", acct, "-s", SERVICE,
                          "-w", password, "-T", SECURITY])
    if rc != 0:
        return False, "写入钥匙串失败：%s" % (err or "unknown")
    return True, ""


def load_password(account=None):
    """读钥匙串；失败返回空串。"""
    acct = account or os.environ.get("USER") or "user"
    rc, out, _err = _run(["find-generic-password", "-w", "-s", SERVICE, "-a", acct])
    return out if rc == 0 else ""


def delete_password(account=None):
    acct = account or os.environ.get("USER") or "user"
    rc, _out, _err = _run(["delete-generic-password", "-s", SERVICE, "-a", acct])
    return rc == 0


def has_password(account=None):
    return bool(load_password(account))


# ---------------------------------------------------------------- 注入

def type_text(text, enter=False):
    """用真实键码敲出字符串（含结尾回车）。

    实测：NSTextView（含锁屏的密码框）不接受 CGEventKeyboardSetUnicodeString
    合成的文本事件，只有带键码的按键才会被当成输入；所以这里按 US 布局查表。
    副作用：中文输入法激活时可能进候选框，因此密码要求纯 ASCII。
    """
    from Quartz import (CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
                        kCGEventFlagMaskShift, CGEventSetFlags, CGEventSetType,
                        kCGEventFlagsChanged)
    ok = True
    for ch in str(text or ""):
        pair = KEYMAP.get(ch)
        if pair is None:
            ok = False
            continue
        code, shift = pair
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, code, down)
            # 每个事件都显式写 flags：不写时它继承上一个事件的修饰键状态，
            # 结果 Shift 会“粘住”（实测 Test456! -> TEST$%^!）。
            CGEventSetFlags(ev, kCGEventFlagMaskShift if shift else 0)
            CGEventPost(kCGHIDEventTap, ev)
    if enter:
        for down in (True, False):
            CGEventPost(kCGHIDEventTap, CGEventCreateKeyboardEvent(None, RETURN_KEY, down))
    return ok


# ---------------------------------------------------------------- 判定

class UnlockDecider:
    """平板“从无到有”且屏幕锁着 -> 等 delay 秒后触发一次。

    限制：冷却 cooldown 秒内不重复；同一把锁只触发一次（解锁失败不会反复试）。
    """

    def __init__(self, delay=2.0, cooldown=60.0):
        self.delay = float(delay)
        self.cooldown = float(cooldown)
        self._prev_present = None
        self._since = None
        self._last_fire = -1e9

    def update(self, now, present, locked):
        present = bool(present)
        edge = (self._prev_present is False and present is True)
        self._prev_present = present
        if edge and locked:
            self._since = now
            return False
        if self._since is None:
            return False
        if not present or not locked:
            self._since = None
            return False
        if now - self._since < self.delay:
            return False
        if now - self._last_fire < self.cooldown:
            self._since = None
            return False
        self._last_fire = now
        self._since = None
        return True


def selftest_offline():
    ok, bad = 0, []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    ck("允许 ASCII 与中文", not unsupported_chars("abc密码!@#"))
    ck("拒绝换行", unsupported_chars("a\nb") == ["\n"])
    d = UnlockDecider(delay=2, cooldown=60)
    ck("启动时就插着不触发", not d.update(0, True, True))
    ck("先拔掉", not d.update(1, False, True))
    ck("再插上开始计时", not d.update(2, True, True))
    ck("未到时间", not d.update(3.5, True, True))
    ck("到时间触发", d.update(4.1, True, True))
    ck("冷却内不重复", not d.update(5, False, True) and not d.update(6, True, True)
       and not d.update(12, True, True))
    d2 = UnlockDecider(delay=2, cooldown=0)
    ck("屏幕未锁不触发", not d2.update(0, True, False) and not d2.update(5, True, False))
    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest_offline(), ensure_ascii=False))
