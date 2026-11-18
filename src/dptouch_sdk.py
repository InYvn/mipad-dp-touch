#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dptouch_sdk —— 给「AI 生成插件」用的受限宿主接口。

设计边界：
- 不暴露 DPApp、HID 句柄、输入引擎、文件系统或任意模块导入；
- 设置操作全部走 AISettingsBridge（白名单 + 校验 + 读回验证 + 回滚）；
- 只提供读取类信息（前台 App、屏幕）与自己的数据目录。

AI 生成的插件只能 import：
    AppKit / Foundation / objc / json / math / time / datetime / re
    typing / dataclasses / dptouch_sdk
"""

import json
import os

import dptouch_plugin as _PLUG


def _bridge():
    host = _PLUG.host()
    api = getattr(host, "settings_api", None) if host is not None else None
    if api is None:
        raise RuntimeError("宿主没有提供设置接口")
    return api


def _result_dict(res):
    if res is None:
        return {"ok": False, "errors": ["没有可撤销的修改"], "txn": "",
                "applied": [], "rolled_back": [], "rollback_failed": []}
    return {
        "ok": bool(res.ok), "txn": res.txn, "undo_of": getattr(res, "undo_of", ""),
        "errors": list(res.errors), "rolled_back": list(res.rolled_back),
        "rollback_failed": list(res.rollback_failed),
        "applied": [{"key": c.key, "title": c.title, "old": c.old, "new": c.new}
                    for c in res.applied],
    }


# ---------------------------------------------------------------- 设置
def settings_manifest():
    """可修改设置的白名单：[{op,key,title,values,current}]。"""
    return _bridge().manifest()


def settings_context():
    """给模型/用户看的能力说明文本。"""
    return _bridge().context_text()


def settings_preview(ops):
    """预览：不修改任何状态。"""
    return [{"key": c.key, "title": c.title, "old": c.old, "new": c.new,
             "warn": c.warn, "implicit": c.implicit}
            for c in _bridge().preview(list(ops))]


def settings_apply(ops):
    """执行 + 读回验证；失败自动回滚。"""
    return _result_dict(_bridge().apply(list(ops)))


def settings_undo(txn=None):
    """撤销指定事务；txn 为空时撤销最近一笔。"""
    api = _bridge()
    return _result_dict(api.undo(txn) if hasattr(api, "undo") else api.undo_last())


# ---------------------------------------------------------------- 只读信息
def front_app():
    """当前前台 App：{name, bundle}；拿不到返回空字符串。"""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {"name": "", "bundle": ""}
        return {"name": str(app.localizedName() or ""),
                "bundle": str(app.bundleIdentifier() or "")}
    except Exception:
        return {"name": "", "bundle": ""}


def screens():
    """屏幕：[{x,y,w,h,visible}]，坐标为 AppKit 全局坐标。"""
    out = []
    try:
        from AppKit import NSScreen
        for s in NSScreen.screens():
            f = s.frame()
            v = s.visibleFrame()
            out.append({"x": f.origin.x, "y": f.origin.y,
                        "w": f.size.width, "h": f.size.height,
                        "visible": {"x": v.origin.x, "y": v.origin.y,
                                    "w": v.size.width, "h": v.size.height}})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 自己的目录
def data_dir(plugin_id):
    """插件自己的数据目录（会创建）；只允许 ASCII 和数字下划线。"""
    pid = str(plugin_id or "").strip()
    if not pid or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in pid):
        raise ValueError("非法插件 id：%r" % (plugin_id,))
    path = os.path.join(_PLUG.plugins_dir(), pid, "data")
    os.makedirs(path, exist_ok=True)
    return path


def log(text):
    host = _PLUG.host()
    fn = getattr(host, "log", None) if host is not None else None
    if callable(fn):
        try:
            fn("[插件] " + str(text))
            return True
        except Exception:
            pass
    return False


def dump(obj):
    """便于插件调试：返回稳定的 JSON 文本。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
