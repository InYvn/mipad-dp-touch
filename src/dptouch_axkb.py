#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开关系统「无障碍键盘」(Accessibility Keyboard) —— 通过辅助功能 API 驱动系统设置。

背景（都是实测出来的，不是猜的）:
  * 键盘状态可以读: `defaults read com.apple.universalaccess virtualKeyboardOnOff`
  * 但**写不进去** —— 该偏好域受系统保护，写完读回不变（含 killall cfprefsd 刷新缓存）
  * 键盘本体是 `/System/Library/Input Methods/Assistive Control.app`（一个输入法 App），
    它的面板是 NSPanel，**不跟随 App 的隐藏状态** -> 隐藏 App 不能关掉键盘
  * launchctl kill 该服务 = "Not privileged to signal service"
  * 唯一可行的路: 用 AX 打开系统设置的无障碍页 -> 按名字找到「键盘」子项与
    「无障碍键盘」开关 -> AXPress -> 读偏好确认结果

设计取舍:
  * **按名字找，不按索引**。老的开源实现 (sidneys/macos-accessibility-keyboard) 写死
    `tabGroups[0].radioButtons[1]`/`checkboxes[0]`，系统一改版就废；这里全部按
    标题/描述匹配，找不到就如实报错，绝不瞎点。
  * **点完必须读回验证**（偏好键 + 键盘窗口），变了才算成功 —— 不允许"看起来点了"就报成功。
  * 需要调用方（App）持有「辅助功能」授权；没有授权时所有 AX 调用会返回错误码，
    这里统一转成 (False, 原因) 而不抛异常。
"""

import subprocess
import time

try:
    from ApplicationServices import (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                                     AXUIElementPerformAction, AXUIElementSetAttributeValue)
except Exception:                                    # 无 GUI / 测试环境
    AXUIElementCreateApplication = None

KEY_ON = "virtualKeyboardOnOff"
PANE_URL = "x-apple.systempreferences:com.apple.Accessibility-Settings.extension"

# 在界面里按这些名字找（大小写/空格宽松匹配）
SUBITEM_NAMES = ("键盘", "keyboard")
TOGGLE_NAMES = ("无障碍键盘", "accessibility keyboard")

AX_OK = 0
ATTRS = ("AXTitle", "AXDescription", "AXValue", "AXHelp", "AXRole", "AXRoleDescription")


# ---------------------------------------------------------------- 状态读取

def state():
    """键盘当前是否在显示。返回 (bool, 说明)。读不到就给 None。"""
    try:
        r = subprocess.run(["defaults", "read", "com.apple.universalaccess", KEY_ON],
                           capture_output=True, text=True, timeout=5)
        v = r.stdout.strip()
        if v in ("0", "1"):
            return v == "1", "偏好 %s=%s" % (KEY_ON, v)
        return None, "偏好读不到 (%r)" % (v,)
    except Exception as e:
        return None, "读偏好失败: %r" % (e,)


def panel_windows():
    """键盘面板窗口数（AssistiveControl 名下、宽度在 100..1000 之间的那个）。"""
    try:
        from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
                            kCGNullWindowID)
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
        n = 0
        for w in wins:
            if (w.get("kCGWindowOwnerName") or "") != "AssistiveControl":
                continue
            wd = (w.get("kCGWindowBounds") or {}).get("Width", 0)
            if 100 < wd < 1000:
                n += 1
        return n
    except Exception:
        return -1


# ---------------------------------------------------------------- AX 基础

def _attr(el, name):
    try:
        err, val = AXUIElementCopyAttributeValue(el, name, None)
        return val if err == AX_OK else None
    except Exception:
        return None


def _texts(el):
    """元素身上所有能当"名字"用的文本"""
    out = []
    for a in ATTRS:
        v = _attr(el, a)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _children(el):
    v = _attr(el, "AXChildren")
    return list(v) if v else []


def _match(el, needles):
    for t in _texts(el):
        low = t.lower()
        for n in needles:
            if n.lower() in low:
                return t
    return None


def walk(el, depth=0, max_depth=12, sink=None):
    """深度优先遍历（给 dump 用）。sink(role, texts, depth) 每访问一个元素调一次。"""
    role = _attr(el, "AXRole") or "?"
    texts = _texts(el)
    if sink is not None:
        sink(role, texts, depth)
    if depth >= max_depth:
        return
    for c in _children(el):
        walk(c, depth + 1, max_depth, sink)


def find_all(el, needles, max_depth=12, want_roles=None, _anc=()):
    """找出所有匹配的元素，带回它们的祖先角色链（用来区分同名元素）。

    实测: 设置面板里叫「键盘」的有三处 —— 侧栏那个（祖先有 AXRow/AXCell，会跳到别的面板）、
    无障碍内容区那个（祖先有 AXGroup）、菜单里那个（AXMenuItem）。只能靠祖先链区分。
    """
    out = []
    hit = _match(el, needles)
    role = _attr(el, "AXRole") or ""
    if hit and (want_roles is None or role in want_roles):
        out.append((el, hit, _anc))
    if max_depth <= 0:
        return out
    for c in _children(el):
        out += find_all(c, needles, max_depth - 1, want_roles, _anc + (role,))
    return out


def pick_content_item(hits):
    """在若干同名命中里挑「内容区」那个: 祖先有 AXGroup、且没有 AXRow/AXCell。

    挑不出来就返回 None（宁可不点，也不要点错侧栏跳走）。
    """
    best = None
    for el, hit, anc in hits:
        if "AXGroup" in anc and "AXRow" not in anc and "AXCell" not in anc:
            best = (el, hit)          # 取最后一个（内容区在侧栏之后）
    return best


def find_toggle(win):
    """找「无障碍键盘」的开关（返回 (元素, 说明)）。

    实测结构（macOS 27）:
        AXGroup
          AXStaticText 无障碍键盘            <- 标签
          AXStaticText 无障碍键盘可让你…      <- 说明（也含这个词，别拿它）
          AXButton     显示细节
          AXCheckBox   转换                  <- 真开关，标题是「转换」不是「无障碍键盘」
    所以：精确找到标签 -> 从它**同一个父组**里取复选框。
    """
    hits = find_all(win, ("无障碍键盘",), max_depth=12, want_roles=("AXStaticText",))
    for el, hit, _anc in hits:
        if hit.strip() != "无障碍键盘":            # 精确匹配，跳过说明文字
            continue
        parent = _attr(el, "AXParent")
        if parent is None:
            continue
        for c in _children(parent):
            if (_attr(c, "AXRole") or "") == "AXCheckBox":
                return c, "标签「%s」同组内的复选框" % hit
    return None, None


def _walk_flat(el, max_depth=8):
    """把子树拉平成列表（找按钮用）"""
    out = [el]
    if max_depth <= 0:
        return out
    for c in _children(el):
        out += _walk_flat(c, max_depth - 1)
    return out


def find(el, needles, max_depth=12, want_roles=None):
    """按名字找元素（深度优先，先自己后孩子）。找不到返回 None。"""
    hit = _match(el, needles)
    role = _attr(el, "AXRole") or ""
    if hit and (want_roles is None or role in want_roles):
        return el, hit
    if max_depth <= 0:
        return None, None
    for c in _children(el):
        r, h = find(c, needles, max_depth - 1, want_roles)
        if r is not None:
            return r, h
    return None, None


def press(el):
    """按一下（优先 AXPress，退而求其次 AXConfirm）"""
    for act in ("AXPress", "AXConfirm"):
        try:
            err = AXUIElementPerformAction(el, act)
            if err == AX_OK:
                return True, act
        except Exception:
            pass
    return False, "AXPress/AXConfirm 都失败"


# ---------------------------------------------------------------- 设置窗口

def settings_pid():
    """系统设置的进程号（没有就返回 None）"""
    try:
        out = subprocess.run(["pgrep", "-x", "System Settings"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except Exception:
        return None


def quit_settings():
    """把系统设置整个退掉（测试用：避免在多个旧窗口里操作）"""
    try:
        subprocess.run(["osascript", "-e", 'tell application "System Settings" to quit'],
                       capture_output=True, timeout=10)
        time.sleep(1.5)
    except Exception:
        pass


def open_pane():
    """打开系统设置的「无障碍」页（已经开着就复用）"""
    try:
        subprocess.run(["open", PANE_URL], capture_output=True, timeout=10)
    except Exception as e:
        return None, "打不开设置面板: %r" % (e,)
    for _ in range(20):                      # 最多等 5 秒
        pid = settings_pid()
        if pid:
            return pid, ""
        time.sleep(0.25)
    return None, "系统设置没有起来"


def dump(pid, max_depth=12):
    """把设置窗口的 AX 树 dump 成行列表（自检用）"""
    if AXUIElementCreateApplication is None:
        return ["AX API 不可用"]
    app = AXUIElementCreateApplication(pid)
    lines = []

    def sink(role, texts, depth):
        keep = [t for t in texts if t and not t.startswith("AX")]
        if keep or role in ("AXWindow", "AXGroup", "AXCheckBox", "AXRadioButton", "AXButton"):
            lines.append("%s%s | %s" % ("  " * depth, role, " / ".join(keep[:3])))

    walk(app, 0, max_depth, sink)
    return lines


def close_pane(was_running):
    """用完把设置窗口收掉（用户原本没开设置才关，开了就别动）"""
    if was_running:
        return
    try:
        subprocess.run(["osascript", "-e", 'tell application "System Settings" to quit'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程

def toggle(verbose=None):
    """切换无障碍键盘。返回 (ok, 说明)。

    ok=False 时说明里一定写清是哪一步没成（没授权 / 找不到子项 / 找不到开关 / 点了没变化）。
    """
    def say(msg):
        if verbose:
            verbose(msg)

    if AXUIElementCreateApplication is None:
        return False, "AX API 不可用（缺 pyobjc ApplicationServices）"

    before, why = state()
    if before is None:
        return False, "读不到键盘状态: %s" % why
    say("切换前: 键盘%s (%s)" % ("在显示" if before else "已隐藏", why))

    was_running = settings_pid() is not None
    import os as _os
    if _os.environ.get("DPTOUCH_AXKILL"):
        quit_settings()
        was_running = False
    pid, err = open_pane()
    if pid is None:
        return False, err
    say("系统设置 pid=%d（原本%s在运行）" % (pid, "" if was_running else "没"))

    app = AXUIElementCreateApplication(pid)
    win = None
    for _ in range(24):                      # 最多等 6 秒让窗口出现
        wins = _attr(app, "AXWindows") or []
        for w in wins:
            if (_attr(w, "AXTitle") or "").strip():
                win = w
                break
        if win is not None:
            break
        time.sleep(0.25)
    if win is None:
        close_pane(was_running)
        return False, "设置窗口没出现"

    # 1) 先切到「键盘」子页。★必须挑内容区那个：侧栏也有个同名的「键盘」，点了会跳到别的面板
    hits = find_all(win, SUBITEM_NAMES, max_depth=10)
    say("叫「键盘」的元素有 %d 个" % len(hits))
    picked = pick_content_item(hits)
    if picked is not None:
        sub, hit = picked
        ok, how = press(sub)
        say("点内容区的「%s」子项: %s (%s)" % (hit, "成功" if ok else "失败", how))
        time.sleep(1.3)
    else:
        say("没找到内容区的「键盘」子项（只有侧栏/菜单里的同名项）—— 直接在当前页找开关")

    # 2) 找开关并按它（按标签定位到同组的 AXCheckBox —— 直接按名字会按到文字标签上）
    box, hit2 = find_toggle(win)
    if box is None:
        close_pane(was_running)
        return False, "在设置面板里找不到「无障碍键盘」开关（界面结构可能变了）"
    role = _attr(box, "AXRole") or "?"
    val_before = _attr(box, "AXValue")
    ok, how = press(box)
    say("按开关(%s, %s, 当前值=%s): %s (%s)" % (role, hit2, val_before,
                                              "成功" if ok else "失败", how))
    if not ok:
        close_pane(was_running)
        return False, "开关按不动: %s" % how

    # 2.5) 关闭键盘时会弹确认框（"你想要先关闭无障碍键盘吗？"）—— 必须点掉，否则设置不落地
    for _ in range(10):
        sheets = _attr(win, "AXSheets") or []
        if sheets:
            sh = sheets[0]
            btns = [b for b in _walk_flat(sh, 6) if (_attr(b, "AXRole") or "") == "AXButton"]
            labels = [_attr(b, "AXTitle") for b in btns]
            say("出现确认对话框，按钮: %s" % labels)
            if btns:
                ok, how = press(btns[0])          # 第一个 = 确认
                say("点确认: %s (%s)" % ("成功" if ok else "失败", how))
            break
        time.sleep(0.3)

    # 3) 读回验证 —— 变了才算成功
    time.sleep(1.6)
    after, why2 = state()
    panel = panel_windows()
    say("切换后: 键盘%s (%s) | 面板窗口 %d" % ("在显示" if after else "已隐藏", why2, panel))
    close_pane(was_running)

    if after is None:
        return False, "点了但读不回状态，无法确认（%s）" % why2
    if after == before:
        return False, "点了开关但状态没变（仍是%s）—— 界面结构可能变了，没敢当成成功" % (
            "开" if before else "关")
    return True, "已%s无障碍键盘" % ("打开" if after else "关闭")
