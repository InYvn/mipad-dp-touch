"""显示缩放 (HiDPI) —— **只动「平板那块屏」**

背景: 平板面板 3408x2272, 以 1x 送过去时字只有 MacBook Air 13" 的一半大 (实测 ≈308 vs
≈123 逻辑 PPI); 直接降分辨率会被平板二次放大 → 糊。正解是 macOS 早就生成好的 HiDPI 档:
按 2x 渲染、帧缓冲仍是面板原生的 3408x2272 → 字放大 2 倍且像素 1:1。

多屏安全 (本模块的硬约束, 有测试钉住):
  1. 所有 CG 调用都**显式带 displayID**; 本文件里不出现 CGMainDisplayID (test_display.py 静态检查)。
  2. 只改目标屏的 mode, 不碰 CGConfigureDisplayOrigin / 镜像 / 排列 → 其它屏完全不受影响。
  3. 认不出「平板那块屏」就拒绝执行, 除非用户显式打开 allow_unverified。
  4. 切换必须走配置事务 + kCGConfigurePermanently: 单独 CGDisplaySetDisplayMode 会被系统按
     存档配置在 1 秒内弹回 (实测, 连切普通 1600x900 也一样)。
  5. 切换后读回校验; 被弹回则自动恢复切换前那档, 并如实报告失败。
"""

import time

import Quartz

try:
    import AppKit
except Exception:                      # 无 GUI 环境 (测试) 也能 import
    AppKit = None

# 实测: 小米平板 9 Pro Max 以 DP-in 出现在 Mac 上时的 EDID 身份
#   (vendor, model, EDID 名, 是否已实测验证)
KNOWN_DISPLAYS = [
    (25001, 4, "MI DISPLAY", True),
]
NAME_HINTS = ("MI DISPLAY", "XIAOMI", "MI PAD", "小米")
PANEL_NATIVE = (3408, 2272)            # 该面板原生分辨率 (帧缓冲到达即 1:1)
ASPECT_TOL = 0.02                      # 只列与面板同比例的档位 (否则留黑边/拉伸)


# --------------------------------------------------------------------------
# 枚举 (纯读, 不改任何东西)
# --------------------------------------------------------------------------

def _screen_names():
    """displayID -> 用户可见名字 (来自 NSScreen, 比 IOKit 那条路简单)"""
    names = {}
    if AppKit is None:
        return names
    try:
        for s in AppKit.NSScreen.screens():
            num = s.deviceDescription().get("NSScreenNumber")
            if num is not None:
                names[int(num)] = str(s.localizedName())
    except Exception:
        pass
    return names


def match_kind(info):
    """'known' 已实测 | 'likely' 同族但没验证过 | '' 不像平板那块屏"""
    if not info:
        return ""
    for v, mo, _n, ok in KNOWN_DISPLAYS:
        if info.get("vendor") == v and info.get("model") == mo:
            return "known" if ok else "likely"
    up = (info.get("name") or "").upper()
    if any(h in up for h in NAME_HINTS):
        return "likely"
    return ""


def displays():
    """当前所有活动显示器 (每块屏独立取信息)"""
    out = []
    try:
        err, dids, _cnt = Quartz.CGGetActiveDisplayList(16, None, None)
    except Exception:
        return out
    if err != 0:
        return out
    names = _screen_names()
    for did in dids:
        did = int(did)
        info = _one(did)
        info["name"] = names.get(did) or info["name"]
        info["match"] = match_kind(info)
        out.append(info)
    return out


def _one(did):
    m = Quartz.CGDisplayCopyDisplayMode(did)
    lg = (Quartz.CGDisplayModeGetWidth(m), Quartz.CGDisplayModeGetHeight(m))
    fb = (Quartz.CGDisplayModeGetPixelWidth(m), Quartz.CGDisplayModeGetPixelHeight(m))
    return {
        "id": did,
        "name": "显示器 %d" % did,
        "vendor": int(Quartz.CGDisplayVendorNumber(did)),
        "model": int(Quartz.CGDisplayModelNumber(did)),
        "serial": int(Quartz.CGDisplaySerialNumber(did)),
        "builtin": bool(Quartz.CGDisplayIsBuiltin(did)),
        "main": bool(Quartz.CGDisplayIsMain(did)),
        "logical": lg,
        "fb": fb,
        "hz": round(Quartz.CGDisplayModeGetRefreshRate(m), 1),
        "hidpi": fb[0] == 2 * lg[0] and fb[1] == 2 * lg[1],
    }


def by_id(did):
    for d in displays():
        if d["id"] == int(did):
            return d
    return None


def panel_native(info):
    """这块屏的面板原生分辨率 (用于判断「帧缓冲 1:1」)"""
    if info and (info.get("vendor"), info.get("model")) == (KNOWN_DISPLAYS[0][0],
                                                            KNOWN_DISPLAYS[0][1]):
        return PANEL_NATIVE
    if info and info.get("fb"):
        return tuple(info["fb"])
    return PANEL_NATIVE


# --------------------------------------------------------------------------
# 目标屏选择 —— 多屏环境下的第一道保险
# --------------------------------------------------------------------------

def pick_target(allow_unverified=False, infos=None):
    """从所有屏里挑出平板那块。返回 (info|None, 原因)。

    恰好多于一块匹配 -> 拒绝 (宁可不动, 也不猜)。
    """
    infos = displays() if infos is None else infos
    hit = [d for d in infos if not d.get("builtin")
           and (d.get("match") or match_kind(d)) in ("known", "likely")]
    if not hit:
        return None, "没找到平板那块屏 (已连接 %d 块显示器)" % len(infos)
    if len(hit) > 1:
        return None, "匹配到 %d 块疑似平板的屏, 不猜 → 拒绝改动" % len(hit)
    ok, why = guard(hit[0], allow_unverified)
    if not ok:
        return None, why
    return hit[0], ""


def guard(info, allow_unverified=False):
    """纯函数: 这块屏能不能动。返回 (ok, 原因)"""
    if info is None:
        return False, "没有可操作的目标屏"
    if info.get("builtin"):
        # Mac 自带屏永不改: 改坏了连菜单都点不到, 没法自救
        return False, "「%s」是 Mac 自带屏, 不动 (改坏了自己这块没法救)" % info.get("name", "?")
    kind = info.get("match") or match_kind(info)
    if kind == "known":
        return True, ""
    if kind == "likely":
        if allow_unverified:
            return True, ""
        return False, "「%s」像小米屏但未实测验证 —— 勾选「允许管理未识别的显示器」后再试" % \
            info.get("name", "?")
    return False, "「%s」不是平板那块屏, 拒绝改动 (多屏保护)" % info.get("name", "?")


# --------------------------------------------------------------------------
# 档位枚举
# --------------------------------------------------------------------------

def modes(did, include_hidden_hidpi=True):
    """该屏所有可用档位。默认打开隐藏的 HiDPI 变体 (系统默认不给)。"""
    opts = {}
    if include_hidden_hidpi:
        opts[Quartz.kCGDisplayShowDuplicateLowResolutionModes] = True
    try:
        raw = Quartz.CGDisplayCopyAllDisplayModes(did, opts) or []
    except Exception:
        raw = []
    out = []
    usable_fn = getattr(Quartz, "CGDisplayModeIsUsableForDesktopGUI", None)
    for m in raw:
        if usable_fn is not None and not usable_fn(m):
            continue
        lg = (Quartz.CGDisplayModeGetWidth(m), Quartz.CGDisplayModeGetHeight(m))
        fb = (Quartz.CGDisplayModeGetPixelWidth(m), Quartz.CGDisplayModeGetPixelHeight(m))
        hz = round(Quartz.CGDisplayModeGetRefreshRate(m), 1)
        out.append({
            "w": lg[0], "h": lg[1], "pw": fb[0], "ph": fb[1], "hz": hz,
            "hidpi": fb[0] == 2 * lg[0] and fb[1] == 2 * lg[1],
            "ref": m,
        })
    return out


def current(did):
    """当前档位 (只读)"""
    m = Quartz.CGDisplayCopyDisplayMode(did)
    lg = (Quartz.CGDisplayModeGetWidth(m), Quartz.CGDisplayModeGetHeight(m))
    fb = (Quartz.CGDisplayModeGetPixelWidth(m), Quartz.CGDisplayModeGetPixelHeight(m))
    return {"w": lg[0], "h": lg[1], "pw": fb[0], "ph": fb[1],
            "hz": round(Quartz.CGDisplayModeGetRefreshRate(m), 1),
            "hidpi": fb[0] == 2 * lg[0] and fb[1] == 2 * lg[1]}


def options(did, panel=PANEL_NATIVE, tol=ASPECT_TOL, aspect_only=True):
    """按逻辑尺寸归并的 HiDPI 档位 (同尺寸取最高刷新率), 大小降序。

    aspect_only: 只留与面板同比例的 —— 3:2 面板上的 4:3 / 16:9 档会留黑边或拉伸。
    每项补 native (帧缓冲 == 面板原生 = 零缩放 = 最锐) 与 upscale (平板放大倍数)。
    """
    best = {}
    for m in modes(did):
        if not m["hidpi"]:
            continue
        if aspect_only and m["h"] and abs(m["w"] / float(m["h"]) -
                                          panel[0] / float(panel[1])) > tol:
            continue
        k = (m["w"], m["h"])
        if k not in best or m["hz"] > best[k]["hz"]:
            best[k] = m
    out = sorted(best.values(), key=lambda m: (-m["w"], -m["h"]))
    for m in out:
        _annotate(m, panel)
    return out


def _annotate(m, panel=PANEL_NATIVE):
    m["native"] = (m["pw"], m["ph"]) == (panel[0], panel[1])
    m["upscale"] = round(panel[0] / float(m["pw"]), 2) if m["pw"] else 0
    return m


def label(m, panel=PANEL_NATIVE):
    """菜单里显示的一行"""
    m = _annotate(dict(m), panel)
    tag = "像素 1:1 最锐" if m["native"] else "平板放大 %gx" % m["upscale"]
    hz = "" if m["hz"] <= 0 else ", %gHz" % m["hz"]
    return "UI %d×%d  (%s%s)" % (m["w"], m["h"], tag, hz)


def label_short(m, panel=PANEL_NATIVE):
    """窗口下拉里的一行: 不带括号(用户嫌括号多), 用 · 分隔"""
    m = _annotate(dict(m), panel)
    tag = "像素 1:1 最锐" if m["native"] else "平板放大 %gx" % m["upscale"]
    hz = "" if m["hz"] <= 0 else " · %gHz" % m["hz"]
    return "%d×%d · %s%s" % (m["w"], m["h"], tag, hz)


def find_mode(did, w, h, hz=None, hidpi=None):
    """在枚举结果里找一档 (恢复用)。找不到返回 None"""
    cands = [m for m in modes(did) if m["w"] == w and m["h"] == h
             and (hz is None or abs(m["hz"] - hz) < 1.0)
             and (hidpi is None or m["hidpi"] == hidpi)]
    if not cands:
        return None
    return max(cands, key=lambda m: m["hz"])


# --------------------------------------------------------------------------
# 切换 (事务 + 永久)
# --------------------------------------------------------------------------

def switch(did, mode_ref):
    """把**这一块**屏切到 mode_ref。返回 CGError (0 = 成功)。

    只设 mode, 不设 origin/镜像 → 其它屏与屏间排列保持不变。
    """
    err, cfg = Quartz.CGBeginDisplayConfiguration(None)   # PyObjC 顺序: (err, 出参)
    if err != 0:
        return int(err)
    e1 = Quartz.CGConfigureDisplayWithDisplayMode(cfg, int(did), mode_ref, None)
    if e1 != 0:
        Quartz.CGCancelDisplayConfiguration(cfg)
        return int(e1)
    return int(Quartz.CGCompleteDisplayConfiguration(cfg, Quartz.kCGConfigurePermanently))


def apply(did, mode_ref, w, h, hz=0, allow_unverified=False, wait=1.3, tries=2,
          dry_run=False):
    """切档 + 读回校验。被系统弹回则自动恢复切换前那档。

    返回 {ok, err, msg, prev, now}
    """
    info = by_id(did)
    ok, why = guard(info, allow_unverified)
    if not ok:
        return {"ok": False, "err": -1, "msg": why, "prev": None, "now": None}
    prev_ref = Quartz.CGDisplayCopyDisplayMode(did)
    prev = current(did)
    if dry_run:
        return {"ok": True, "err": 0, "msg": "dry-run: 未改动", "prev": prev, "now": prev}
    err = 0
    for _ in range(max(1, tries)):
        err = switch(did, mode_ref)
        if err != 0:
            break
        time.sleep(wait)
        now = current(did)
        if (now["w"], now["h"]) == (w, h) and (hz <= 0 or abs(now["hz"] - hz) < 1.0):
            return {"ok": True, "err": 0,
                    "msg": "已切到 UI %d×%d @%gHz (帧缓冲 %d×%d)"
                           % (now["w"], now["h"], now["hz"], now["pw"], now["ph"]),
                    "prev": prev, "now": now}
    # 失败 / 被弹回
    if err == 0:
        switch(did, prev_ref)                     # 尽量把用户放回原状态
        return {"ok": False, "err": -2, "prev": prev, "now": current(did),
                "msg": "系统把模式弹回了, 已恢复切换前那档 (UI %d×%d)" % (prev["w"], prev["h"])}
    return {"ok": False, "err": err, "prev": prev, "now": None,
            "msg": "系统拒绝了这个档位 (CGError %d)" % err}


def summary(infos=None):
    """给菜单/诊断用的概况: (目标屏, 原因, 其它屏列表)"""
    infos = displays() if infos is None else infos
    tgt, why = pick_target(infos=infos)
    others = [d for d in infos if tgt is None or d["id"] != tgt["id"]]
    return tgt, why, others
