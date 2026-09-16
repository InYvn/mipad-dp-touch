#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机自启 (登录时自动启动)。

两条实现路径, 优先官方那条:

1. **SMAppService.mainApp** (macOS 13+, 要求进程跑在 .app bundle 里)
   官方登录项 —— 会出现在「系统设置 > 通用 > 登录项与扩展」里, 用户自己也能开关;
   登记后系统会在下次登录时拉起这个 App。
2. **LaunchAgent plist** (兜底: 开发态 / SM 登记被系统拒 / macOS 太老)
   写 ~/Library/LaunchAgents/<label>.plist, 再 launchctl bootstrap gui/<uid> 装载。

对外四个函数: status() / status_line() / enable() / disable() / open_login_items()。
一律返回 (ok, 说明) 而**不抛异常** —— 失败的原因要能显示在菜单上, 不能让菜单崩。
所有副作用 (plist 路径 / 要拉起的命令行 / launchctl 调用 / SMAppService 对象) 都可注入,
方便离线单测, 见 test_autostart.py。
"""

import os
import plistlib
import subprocess
import sys

LABEL = "io.github.inyvn.xiaomi-dptouch"   # 保持与 CFBundleIdentifier 一致: 改 bundle id 会让 TCC 授权失效
APP_NAME = "Mipad DP Touch"

# SMAppService.status()
SM_NOT_REGISTERED, SM_ENABLED, SM_REQUIRES_APPROVAL, SM_NOT_FOUND = 0, 1, 2, 3
SM_STATUS_TEXT = {
    SM_NOT_REGISTERED: "未登记",
    SM_ENABLED: "已开启",
    SM_REQUIRES_APPROVAL: "待你在系统设置里批准",
    SM_NOT_FOUND: "系统找不到这个 App",
}

LOGIN_ITEMS_URLS = [
    "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.users?LoginItems",
]

_log_fn = [lambda m: None]


def set_logger(fn):
    """由 dptouch.py 注入日志函数 (避免模块间 import 环)。"""
    _log_fn[0] = fn


def _log(msg):
    try:
        _log_fn[0]("自启: " + msg)
    except Exception:
        pass


# ---------------------------------------------------------------- 基本事实

def is_frozen():
    """打包成 .app 运行 (PyInstaller) 时为 True。"""
    return bool(getattr(sys, "frozen", False))


def app_arguments():
    """登录时要拉起的命令行。

    打包后 = bundle 里的可执行文件本身; 开发态 = 解释器 + 脚本路径 (只用于测试)。
    """
    if is_frozen():
        try:
            from Foundation import NSBundle
            exe = NSBundle.mainBundle().executablePath()
            if exe:
                return [exe]
        except Exception:
            pass
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0])]


def agent_plist_path(home=None):
    base = home or os.path.expanduser("~")
    return os.path.join(base, "Library", "LaunchAgents", LABEL + ".plist")


def agent_plist_bytes(args):
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": list(args),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    })


# ---------------------------------------------------------------- SMAppService

def _sm_main_app():
    """拿 SMAppService.mainApp 对象。返回 (svc_or_None, 原因)。"""
    if not is_frozen():
        return None, "当前不是 .app bundle (开发态)"
    try:
        from ServiceManagement import SMAppService
    except Exception as e:
        return None, "ServiceManagement 不可用: %r" % (e,)
    try:
        return SMAppService.mainAppService(), ""
    except Exception as e:
        return None, "SMAppService.mainApp 取不到: %r" % (e,)


def _sm_status(svc):
    try:
        return int(svc.status())
    except Exception:
        return -1


def _sm_register(svc):
    """返回 (ok, 说明)。PyObjC 的 *AndReturnError_ 返回 (bool, err) 或 bool。"""
    try:
        r = svc.registerAndReturnError_(None)
    except Exception as e:
        return False, "%r" % (e,)
    if isinstance(r, tuple):
        err = r[1] if len(r) > 1 else None
        return bool(r[0]), ("" if not err else "%s" % err)
    return bool(r), ""


def _sm_unregister(svc):
    try:
        r = svc.unregisterAndReturnError_(None)
    except Exception as e:
        return False, "%r" % (e,)
    if isinstance(r, tuple):
        err = r[1] if len(r) > 1 else None
        return bool(r[0]), ("" if not err else "%s" % err)
    return bool(r), ""


# ---------------------------------------------------------------- launchctl

def _default_runner(cmd):
    """跑一条命令, 返回 (rc, 输出)。永不抛异常。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()
    except Exception as e:
        return -1, "%r" % (e,)


# ---------------------------------------------------------------- 对外接口

def status(plist_path=None, svc=None, use_sm=True):
    """查询实际状态。返回 {enabled, backend, detail}。

    以**系统实际状态**为准 (用户可能在系统设置里改过), 不看我们自己的 config。
    """
    path = plist_path or agent_plist_path()
    agent = os.path.exists(path)
    if use_sm and svc is None:
        svc, _why = _sm_main_app()
    if svc is not None:
        st = _sm_status(svc)
        if st in (SM_ENABLED, SM_REQUIRES_APPROVAL):
            return {"enabled": True, "backend": "sm",
                    "detail": "登录项 %s" % SM_STATUS_TEXT.get(st, st),
                    "needs_approval": st == SM_REQUIRES_APPROVAL}
        if st == SM_NOT_REGISTERED and agent:
            return {"enabled": True, "backend": "agent", "detail": "LaunchAgent"}
        return {"enabled": False, "backend": "sm",
                "detail": SM_STATUS_TEXT.get(st, "状态码 %s" % st)}
    return {"enabled": bool(agent),
            "backend": "agent" if agent else "none",
            "detail": "LaunchAgent" if agent else "未开启"}


def status_line(d=None):
    d = d or status()
    if d["enabled"]:
        return "登录时自动启动  (%s)" % d["detail"]
    return "登录时自动启动  (点我开启)"


def enable(plist_path=None, args=None, runner=None, svc=None, use_sm=True):
    """打开开机自启。返回 (ok, 说明, backend)。SM 走不通时自动退回 LaunchAgent。"""
    path = plist_path or agent_plist_path()
    runner = runner or _default_runner
    if use_sm and svc is None:
        svc, why = _sm_main_app()
        if svc is None:
            _log("登录项不可用 (%s), 走 LaunchAgent" % why)
    if svc is not None:
        ok, err = _sm_register(svc)
        st = _sm_status(svc)
        if st in (SM_ENABLED, SM_REQUIRES_APPROVAL) or (ok and st != SM_NOT_FOUND):
            detail = "登录项 %s" % SM_STATUS_TEXT.get(st, st)
            _log("已登记为登录项 -> %s" % detail)
            return True, detail, "sm"
        _log("登录项登记没成功 (ok=%s err=%s status=%s), 退回 LaunchAgent" % (ok, err, st))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(agent_plist_bytes(args or app_arguments()))
    except Exception as e:
        return False, "写 %s 失败: %r" % (path, e), "none"
    rc, out = runner(["launchctl", "bootstrap", "gui/%d" % os.getuid(), path])
    if rc != 0 and "already" not in out.lower():
        return False, "launchctl bootstrap 失败 (rc=%d): %s" % (rc, out), "none"
    _log("已装 LaunchAgent: %s" % path)
    return True, "LaunchAgent %s" % path, "agent"


def disable(plist_path=None, runner=None, svc=None, use_sm=True):
    """关掉开机自启 (两条路径都清一遍, 幂等)。返回 (ok, 说明)。"""
    path = plist_path or agent_plist_path()
    runner = runner or _default_runner
    msgs = []
    ok = True
    if use_sm and svc is None:
        svc, _why = _sm_main_app()
    if svc is not None:
        st = _sm_status(svc)
        if st in (SM_ENABLED, SM_REQUIRES_APPROVAL):
            r, err = _sm_unregister(svc)
            msgs.append("登录项已注销" if r or _sm_status(svc) == SM_NOT_REGISTERED
                        else "登录项注销失败: %s" % err)
            if not r and _sm_status(svc) != SM_NOT_REGISTERED:
                ok = False
    if os.path.exists(path):
        rc, out = runner(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
        try:
            os.remove(path)
            msgs.append("已移除 LaunchAgent")
        except Exception as e:
            ok = False
            msgs.append("删 plist 失败: %r" % (e,))
        if rc != 0 and "no such process" not in out.lower() and "not found" not in out.lower():
            _log("launchctl bootout rc=%d: %s" % (rc, out))
    if not msgs:
        msgs.append("本来就是关闭的")
    return ok, "; ".join(msgs)


def set_enabled(want, **kw):
    """按菜单那一个复选框的意思办事: want=True -> enable, False -> disable。"""
    if want:
        ok, msg, _backend = enable(**kw)
        return ok, msg
    return disable(**kw)


def open_login_items():
    """打开「系统设置 > 通用 > 登录项」, 供用户自己确认/批准。"""
    for u in LOGIN_ITEMS_URLS:
        try:
            r = subprocess.run(["open", u], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False
