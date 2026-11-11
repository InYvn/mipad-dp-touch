#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EDID 几何修正 (B) —— 纯 Python 校验/几何 + 原生助手控制器。

背景与依据 (见 tools/edid-ab-evidence/ab-1793854557/REPORT.md):
  小米平板 9 Pro Max 以 DP-in 当外接屏时, EDID 报的物理尺寸 (590×340 mm) 与
  macOS 27.3 原生直接触摸的坐标系不一致, 手指落点系统性偏移 (5 点平均 305.4pt)。
  只把 EDID 的物理几何改成 DTD=282×188 mm / 基础块 28×19 cm 并重算校验和
  (变量 B), 其余 384 字节、全部身份/时序/扩展块原样保留, 实测 5 点平均误差
  降到 9.5pt。这些尺寸是**实测有效的参数**, 不是独立测量的硬件尺寸。

本模块职责:
  1. 纯函数: EDID 校验 (multiples128 + extension-count + 每块校验和) / 解析 /
     几何补丁白名单 / 目标屏解析 (身份+名字+时序, 唯一匹配, 绝不写死序列号)。
  2. EDIDController: 管理原生助手子进程 (guard 协议) 的生命周期 ——
     · 助手持有原生 IOAV 目标句柄, 父进程 stdin EOF/崩溃/正常退出时经**原句柄**恢复;
     · 应用前解析保留服务身份并对照工厂 EDID; 有第三方残留 override 一律拒绝覆盖;
     · 成功以**有效几何读回**为准, API rc 成功不算数; 恢复后也要读回验证;
     · 拓扑变化 (拔插/睡眠唤醒) 只安全重解析并重应用一次; 歧义拒绝; 失败退避,
       绝不在每个 UI tick 上反复 apply;
     · 工厂 EDID 备份 + 会话 JSON 状态落在调用方给的支持目录, 不写死任何用户机器路径。
  3. 纯逻辑无 AppKit 依赖, 全部行为由 tools/selftest_edid.py 离线钉住。

坐标: 本模块只动 EDID 几何, 不碰 CGConfigure* (模式/排列/镜像/旋转/刷新率) ——
由测试与代码审查双重钉住 (本文件里不出现 CGConfigure / CGBeginDisplayConfiguration)。
"""

import json
import hashlib
import os
import subprocess
import sys
import time

try:                                   # 无 GUI 环境也能 import (测试)
    import Quartz
except Exception:                      # pragma: no cover
    Quartz = None

# ---------------------------------------------------------------------------
# 目标面板身份 —— 三条同时成立才认 (名字/时序来自 EDID 本体, 不是 CG 枚举序号)
#   ① vendor 25001 (0x61A9) × product 4        ② EDID 名 "MI DISPLAY"
#   ③ 首选 DTD 原生时序 3408×2272                并排除自带屏、要求唯一匹配。
# 不写死任何序列号或 displayID —— 插拔换 ID 后照认。
# ---------------------------------------------------------------------------
TARGET_VENDOR = 25001                   # 0x61A9
TARGET_MODEL = 4
TARGET_NAME = "MI DISPLAY"
TARGET_NATIVE = (3408, 2272)
# Conservative known-firmware profile, independent of per-unit serial/date.
# Must match native KnownFactory; unknown firmware refuses instead of guessing.
FACTORY_PROFILE_SHA256 = "cb9050fc6cad56a5b720e7d1d44e0a06e12e933faa943813878f229c89a0c592"

GEO_MM = (282, 188)                     # 实测有效的补丁参数 (B); 不是独立测量值
BASE_CM = (28, 19)                      # 基础块「最大图像尺寸」按厘米精度
READBACK_TOL_MM = 4.0                   # 有效几何读回的容差 (mm)

BLOCK = 128
MAGIC = bytes.fromhex("00ffffffffffff00")
HELPER_NAME = "vedid"
MAX_MM = 2550


class EDIDError(ValueError):
    """EDID 无效 / 结构不符。"""


# ---------------------------------------------------------------------------
# 校验 / 解析 (纯函数)
# ---------------------------------------------------------------------------

def validate(edid):
    """EDID 全量校验: 头魔数 + 长度 128 倍数 + 扩展数与实际块数一致 + 每块校验和。

    通过返回 None, 否则抛 EDIDError (带原因)。384 字节 (基础+2 扩展) 是合法的。
    """
    if not isinstance(edid, (bytes, bytearray)):
        raise EDIDError("不是字节串")
    n = len(edid)
    if n < BLOCK:
        raise EDIDError("长度 %d < 128" % n)
    if n % BLOCK:
        raise EDIDError("长度 %d 不是 128 的倍数" % n)
    if edid[:8] != MAGIC:
        raise EDIDError("头部魔数不符")
    want_blocks = (edid[126] + 1) * BLOCK
    if n != want_blocks:
        raise EDIDError("扩展数字段=%d (期望 %d 字节), 实际 %d 字节"
                        % (edid[126], want_blocks, n))
    for base in range(0, n, BLOCK):
        if sum(edid[base:base + BLOCK]) % 256:
            raise EDIDError("第 %d 块校验和不对" % (base // BLOCK,))
    return None


def info_of(edid):
    """校验并解析出 (vendor, model, 名字, 首选时序, 基础块 cm)。无效则抛 EDIDError。"""
    validate(edid)
    vendor = (edid[8] << 8) | edid[9]   # CGDisplayVendorNumber = 大端 u16 (0x61A9=25001)
    model = edid[10] | (edid[11] << 8)
    name = _monitor_name(edid)
    pref = _preferred_timing(edid)
    return {"vendor": vendor, "model": model, "name": name,
            "preferred": pref, "base_cm": (edid[21], edid[22]),
            "blocks": len(edid) // BLOCK,
            "serial": int.from_bytes(edid[12:16], "little")}


def _monitor_name(edid):
    """基础块 4 个 18 字节描述符里的 0xFC 显示器名 (去尾随空白)。找不到返回 ""。"""
    for off in (54, 72, 90, 108):
        d = edid[off:off + 18]
        if d[0] == 0 and d[1] == 0 and d[3] == 0xFC:
            raw = bytes(d[5:18])
            end = raw.find(b"\n")
            if end >= 0:
                raw = raw[:end]
            return raw.decode("ascii", "replace").strip()
    return ""


def _preferred_timing(edid):
    """基础块首选 DTD (偏移 54) 的活动像素 (h, v)。不是 DTD 返回 None。"""
    d = edid[54:72]
    if d[0] == 0 and d[1] == 0:
        return None
    h = d[2] | ((d[4] & 0xF0) << 4)
    v = d[5] | ((d[7] & 0xF0) << 4)
    return (h, v)


# ---------------------------------------------------------------------------
# 几何补丁 (只动白名单字节)
# ---------------------------------------------------------------------------

def _dtd_offsets(edid):
    """全部真实 DTD 的起始偏移: 基础块 4 槽 + 每个 CTA-861 扩展的 DTD 表。"""
    out = []
    for off in (54, 72, 90, 108):
        if edid[off] != 0 or edid[off + 1] != 0:
            out.append(off)
    for base in range(BLOCK, len(edid), BLOCK):
        if edid[base] != 0x02:              # CTA-861 tag
            continue
        start = edid[base + 2]
        if start == 0:  # CTA permits an absent DTD collection.
            continue
        if not 4 <= start <= 127:
            raise EDIDError("CTA-861 DTD 偏移非法: %d" % start)
        off = base + start
        while off + 18 <= base + 127:
            if edid[off] == 0 and edid[off + 1] == 0:
                break
            out.append(off)
            off += 18
    return out


def geometry_offsets(edid):
    """补丁允许触碰的字节偏移白名单: 尺寸字段 + 每个被改块的校验和。

    = {21, 22} ∪ {每个真实 DTD 的 +12,+13,+14} ∪ {受影响块的 127}。
    """
    validate(edid)
    allowed = {21, 22}
    for off in _dtd_offsets(edid):
        allowed.update((off + 12, off + 13, off + 14))
        allowed.add((off // BLOCK) * BLOCK + 127)
    allowed.add(127)
    return allowed


def patch_geometry(edid, width_mm, height_mm):
    """只改 EDID 物理几何, 保留全部扩展/时序/身份; 重算受影响块的校验和。

    返回 bytes。无效 EDID / 尺寸出界抛 EDIDError。
    """
    if not all(isinstance(x, int) and not isinstance(x, bool)
               and 1 <= x <= MAX_MM for x in (width_mm, height_mm)):
        raise EDIDError("物理尺寸出 EDID 范围 (1..2550mm): %r×%r" % (width_mm, height_mm))
    validate(edid)
    result = bytearray(edid)
    result[21:23] = bytes((max(1, round(width_mm / 10)), max(1, round(height_mm / 10))))
    changed_blocks = {0}

    def patch_dtd(off):
        if result[off] == 0 and result[off + 1] == 0:
            return
        result[off + 12] = width_mm & 255
        result[off + 13] = height_mm & 255
        result[off + 14] = ((width_mm >> 8) << 4) | (height_mm >> 8)
        changed_blocks.add(off // BLOCK)

    for off in (54, 72, 90, 108):
        patch_dtd(off)
    for base in range(BLOCK, len(edid), BLOCK):
        if edid[base] != 0x02:
            continue
        start = edid[base + 2]
        if start == 0:  # CTA permits an absent DTD collection.
            continue
        if not 4 <= start <= 127:
            raise EDIDError("CTA-861 DTD 偏移非法: %d" % start)
        off = base + start
        while off + 18 <= base + 127 and (edid[off] != 0 or edid[off + 1] != 0):
            patch_dtd(off)
            off += 18
    for block in changed_blocks:
        base = block * BLOCK
        result[base + 127] = (-sum(result[base:base + 127])) & 255
    return bytes(result)


def geometry_only(before, after):
    """after 与 before 的差异是否全部落在几何白名单内。

    返回 (ok, 违规字节列表)。before/after 必须同长且 before 本身合法。
    """
    if len(before) != len(after):
        return False, ["长度不同"]
    validate(before)
    validate(after)
    allowed = geometry_offsets(before)
    bad = sorted(i for i, (a, b) in enumerate(zip(before, after))
                 if a != b and i not in allowed)
    return (not bad), bad


# ---------------------------------------------------------------------------
# 目标屏解析 (多屏安全)
# ---------------------------------------------------------------------------

def find_target(infos, dump):
    """从在线屏里解析出平板那块屏。返回 (info|None, 原因)。

    判据 (缺一不可): 非自带屏 × vendor/product 命中 × EDID 本体名字 "MI DISPLAY"
    × 首选 DTD 3408×2272 × 唯一匹配。dump(did) -> bytes 用于读该屏 EDID
    (真实环境走原生助手 IOAVServiceCopyEDID; 测试注入)。

    同一身份多于一块 -> 拒绝 (宁可不动也不猜); dump 失败 / EDID 无效 / 名字或
    时序不符 -> 一律拒绝。绝不使用 displayID 枚举顺序或序列号。
    """
    infos = list(infos or [])
    cand = [d for d in infos
            if not d.get("builtin")
            and d.get("vendor") == TARGET_VENDOR
            and d.get("model") == TARGET_MODEL]
    if not cand:
        return None, "没找到平板那块屏 (在线 %d 块)" % len(infos)
    if len(cand) > 1:
        return None, "匹配到多块同型号平板屏, 不猜 → 拒绝"
    d = cand[0]
    try:
        raw = dump(d["id"])
    except Exception as e:
        return None, "读不到这块屏的 EDID: %r" % (e,)
    try:
        info = info_of(raw)
    except EDIDError as e:
        return None, "这块屏的 EDID 无效: %s" % e
    if info["vendor"] != d["vendor"] or info["model"] != d["model"] or (
            "serial" in d and info["serial"] != d["serial"]):
        return None, "CG/EDID full identity mismatch"
    if info["name"] != TARGET_NAME:
        return None, "EDID 名 %r 不是 %r" % (info["name"], TARGET_NAME)
    if info["preferred"] != TARGET_NATIVE:
        return None, "原生时序 %r 不是 %r" % (info["preferred"], TARGET_NATIVE)
    out = dict(d)
    out.update(info)
    out["factory_sha256"] = hashlib.sha256(raw).hexdigest()
    return out, ""


# ---------------------------------------------------------------------------
# 助手定位 (运行时绝不写死项目绝对路径; 打包后相对 bundle 找)
# ---------------------------------------------------------------------------

def find_helper():
    """找到可执行的 vedid 助手。找不到返回 None (调用方把特性安全禁用)。

    顺序: 环境变量 (测试注入) -> PyInstaller 打包目录 -> app bundle Resources /
    MacOS -> 源码树构建目录 (相对本文件, 不依赖任何用户机器绝对路径)。
    """
    cands = []
    env = os.environ.get("DPTOUCH_EDID_HELPER")
    if env:
        cands.append(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, HELPER_NAME))
    try:
        from Foundation import NSBundle
        bp = NSBundle.mainBundle().bundlePath()
        for sub in ("Contents/MacOS", "Contents/Resources"):
            cands.append(os.path.join(bp, sub, HELPER_NAME))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [
        os.path.normpath(os.path.join(here, "..", "tools", "native", "build", HELPER_NAME)),
        os.path.normpath(os.path.join(here, "..", "tools", "native", HELPER_NAME)),
        os.path.normpath(os.path.join(here, "..", "build", "native", HELPER_NAME)),
    ]
    for c in cands:
        try:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# 默认环境函数 (真实 Quartz + 助手子进程; 测试注入替身)
# ---------------------------------------------------------------------------

def default_displays():
    """在线屏的 (id, vendor, model, builtin) 列表 (Quartz 只读枚举)。"""
    out = []
    if Quartz is None:
        return out
    try:
        err, dids, _cnt = Quartz.CGGetOnlineDisplayList(128, None, None)
    except Exception:
        return out
    if err != 0 or not dids:
        return out
    for did in dids:
        did = int(did)
        out.append({"id": did,
                    "vendor": int(Quartz.CGDisplayVendorNumber(did)),
                    "model": int(Quartz.CGDisplayModelNumber(did)),
                    "builtin": bool(Quartz.CGDisplayIsBuiltin(did)),
                    "serial": int(Quartz.CGDisplaySerialNumber(did))})
    return out


def default_size(did):
    """CGDisplayScreenSize -> (mm 宽, mm 高); 读不出返回 None。"""
    if Quartz is None:
        return None
    try:
        s = Quartz.CGDisplayScreenSize(int(did))
        if s is None or not (s.width > 0 and s.height > 0):
            return None
        return (float(s.width), float(s.height))
    except Exception:
        return None


def fresh_size(helper, did):
    """Independent read-only process avoids AppKit's pending display-notification cache."""
    try:
        out = _run_helper(helper, ["readback", "--display", str(did)], timeout=4)
        doc = json.loads(out["stdout"])
        mm = doc["sizeMM"]
        w, h = float(mm["w"]), float(mm["h"])
        if out["rc"] != 0 or doc.get("rc") != 0 or int(doc["id"]) != int(did):
            return None
        if not (0 < w <= MAX_MM and 0 < h <= MAX_MM):
            return None
        return w, h
    except Exception:
        return None


def default_dump(helper, did):
    """经原生助手读当前 EDID (IOAVServiceCopyEDID) -> bytes。失败抛异常。"""
    out = _run_helper(helper, ["dump", "--display", str(did), "--out", "-"])
    try:
        doc = json.loads(out["stdout"] or "{}")
        b64 = doc.get("edid")
        if out["rc"] != 0 or not b64:
            raise RuntimeError(out["stderr"].strip() or "vedid dump rc=%d" % out["rc"])
        import base64
        return base64.b64decode(b64)
    except Exception as e:
        raise RuntimeError("vedid dump 解析失败: %r" % (e,))


def _run_helper(helper, argv, timeout=20):
    p = subprocess.run([helper] + [str(a) for a in argv],
                       capture_output=True, text=True, timeout=timeout)
    return {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------

class EDIDController(object):
    """管理位置校正 override 的完整生命周期 (guard 子进程 + 备份 + 会话状态)。

    状态机: disabled / unsupported / ambiguous / checking / applied / failed。
    规则 (与报告约束一一对应):
      · 只有 app 启用 + 原生触控 + 校正开关三者同时为真才管理;
      · 应用以**有效几何读回**为准; API rc=0 但读回没变 -> 判失败并恢复;
      · 恢复后也要读回验证 (尺寸回到工厂值, EDID 与备份逐字节一致);
      · 应用前对照工厂备份, 有第三方残留 override 一律拒绝覆盖;
      · 拓扑变化只重解析+重应用一次; 歧义拒绝; 失败按 backoff 退避,
        绝不在每个 UI tick 上反复 apply;
      · shutdown() 供 App 退出/重启/切开关前调用: 关 stdin -> 助手经
        **原保留句柄**恢复, 并等它报告 + 读回验证。
    """

    def __init__(self, state_dir, log=None, helper=None,
                 displays=None, dump=None, size=None, spawn=None,
                 clock=None, debounce=1.5, backoff=20.0,
                 geom=GEO_MM, tol_mm=READBACK_TOL_MM):
        self.state_dir = state_dir
        self.log = log if log is not None else (lambda m: None)
        self.helper = helper if helper is not None else find_helper()
        self._displays = displays if displays is not None else default_displays
        self._size = size if size is not None else lambda did: fresh_size(self.helper, did)
        self._clock = clock if clock is not None else time.time
        self.debounce = float(debounce)
        self.backoff = float(backoff)
        self.geom = tuple(geom)
        self.tol_mm = float(tol_mm)
        if spawn is not None:
            self._spawn = spawn
        if dump is not None:
            self._dump = dump
        self._helper_ok = None          # None = 还没探过
        self._app_enabled = False
        self._native = False
        self._correct = False
        self._proc = None               # 当前 guard 子进程
        self._state = "disabled"
        self._detail = ""
        self._target = None
        self._backoff_until = 0.0
        self._debounce_until = 0.0
        self._sig = None                # 拓扑签名 (target id, size)
        self._readback_before = None
        self._readback_after = None
        self._applied_at = None
        self._last_error = ""
        self._apply_report = None
        self._restore_report = None
        os.makedirs(self.state_dir, exist_ok=True)

    # ---------------- 环境 (真实实现) ----------------

    def _spawn(self, argv):
        import collections
        import queue
        import threading
        p = subprocess.Popen([self.helper] + [str(a) for a in argv],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace")
        p._edid_lines = queue.Queue(maxsize=16)
        p._edid_stderr = collections.deque(maxlen=16)  # bounded last 16 KiB
        def stdout_reader():
            try:
                while True:
                    line = p.stdout.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536 or not line.endswith("\n"):
                        break  # partial/oversized JSON is never an acknowledgement
                    p._edid_lines.put(line)
            finally:
                p._edid_lines.put("")
        def stderr_reader():
            try:
                while True:
                    chunk = p.stderr.read(1024)
                    if not chunk:
                        break
                    p._edid_stderr.append(chunk)
            except (OSError, ValueError):
                pass
        threading.Thread(target=stdout_reader, daemon=True).start()
        threading.Thread(target=stderr_reader, daemon=True).start()
        return p

    def _dump(self, did):
        return default_dump(self.helper, did)

    # ---------------- 对外接口 ----------------

    def set_desired(self, app_enabled, native_on, correct_on):
        """App 每拍把三个开关的现状送进来。变了会立刻在下一拍生效。"""
        self._app_enabled = bool(app_enabled)
        self._native = bool(native_on)
        self._correct = bool(correct_on)

    def reconfigure(self):
        """手动切开关后调用: 清退避/防抖, 让下一拍立即生效 (不干等)。"""
        self._backoff_until = 0.0
        self._debounce_until = 0.0

    def helper_ok(self):
        """助手是否存在/可执行/自检通过。结果缓存 (每会话探一次)。"""
        if self._helper_ok is None:
            h = self.helper
            if not h or not os.path.isfile(h) or not os.access(h, os.X_OK):
                self._helper_ok = False
            else:
                try:
                    r = _run_helper(h, ["selftest"], timeout=20)
                    doc = json.loads(r["stdout"])
                    self._helper_ok = (r["rc"] == 0 and doc.get("selftest") == "pass")
                except Exception:
                    self._helper_ok = False
        return bool(self._helper_ok)

    def backup_path(self):
        return getattr(self, "_backup_file", os.path.join(self.state_dir, "edid_factory-unresolved.edid"))

    def status_path(self):
        return os.path.join(self.state_dir, "edid_status.json")

    def status(self):
        st = {"state": self._state, "detail": self._detail,
              "helper": self.helper, "helper_ok": self.helper_ok(),
              "target": (self._target or {}).get("name") or None,
              "backup": (self.backup_path()
                         if os.path.exists(self.backup_path()) else None),
              "readback_before": self._readback_before,
              "readback_after": self._readback_after,
              "applied_at": self._applied_at,
              "native_apply": self._apply_report,
              "native_restore": self._restore_report,
              "last_error": self._last_error}
        if self._proc is not None:
            st["guard_pid"] = getattr(self._proc, "pid", None)
        self._write_status(st)
        return st

    def _write_status(self, st):
        """会话 JSON 状态落盘 (状态没变不重写, 免得每秒拍一次磁盘)。"""
        try:
            payload = json.dumps(st, ensure_ascii=False, indent=2, sort_keys=True)
            if payload == getattr(self, "_status_written", None):
                return
            tmp = self.status_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.status_path())
            self._status_written = payload
        except Exception:
            pass

    def _set_state(self, state, detail=""):
        if (state, detail) != (self._state, self._detail):
            self.log("EDID 校正: %s%s" % (state, (" | " + detail) if detail else ""))
        self._state = state
        self._detail = detail
        return self.status()

    # ---------------- 主拍 ----------------

    def tick(self):
        """每拍驱动一次 (由 App 的 1s 定时器调用)。内部全部按状态机短路。"""
        want = self._app_enabled and self._native and self._correct
        if not want:
            if self._proc is not None:
                self._restore("开关已关闭")
            elif self._state not in ("disabled", "failed"):
                self._set_state("disabled", "未启用")
            return self.status()
        if not self.helper_ok():
            if self._proc is not None:
                self._restore("助手不可用")
            return self._set_state("unsupported",
                                   "原生助手不可用 (vedid 未打包/无法执行)")
        now = self._clock()
        if self._proc is not None:
            rc = self._proc.poll()
            if rc is not None:
                self._on_guard_exit()
                return self.status()
            if now < self._debounce_until:
                return self.status()
            # 拓扑签名 (目标 id + 有效几何) 变了 -> 重解析+重应用一次
            if self._topology_changed():
                self.log("EDID 校正: 拓扑变化 (拔插/唤醒), 重解析后重应用一次")
                self._restore("拓扑变化", verify=False)
                return self.status()
            return self.status()
        if now < max(self._backoff_until, self._debounce_until):
            return self.status()
        self._start_guard()
        return self.status()

    # ---------------- 拓扑 ----------------

    def _displays_sig(self):
        """在线屏列表的签名 (id/vendor/model/builtin 排序元组)。读不出返回 None。"""
        try:
            return tuple(sorted((int(d.get("id") or 0),
                                 int(d.get("vendor") or 0),
                                 int(d.get("model") or 0),
                                 bool(d.get("builtin")))
                                for d in self._displays()))
        except Exception:
            return None

    def _topology_changed(self):
        """当前签名 vs 应用时签名: 在线屏列表 + 目标 id + 有效几何尺寸。

        读不出几何 (显示器重配置中) 不算变化 —— 否则 apply 引发的瞬态重配置
        会把自己判成变化, 变成每秒 apply 一次。
        """
        if self._target is None:
            return True
        displays_sig = self._displays_sig()
        if self._sig and displays_sig != self._sig[0]:
            return True
        try:
            size = self._size(self._target["id"])
        except Exception:
            size = None
        if size is None:
            return False
        sig = (displays_sig, self._target["id"],
               tuple(round(v, 1) for v in size))
        if sig != self._sig:
            self._sig = sig
            return True
        return False

    def _same_target_now(self):
        """当前拓扑是否还解析得到同一个目标。返回 (tgt|None, why)。"""
        try:
            return find_target(self._displays(), self._dump)
        except Exception as e:
            return None, "解析目标失败: %r" % (e,)

    # ---------------- 应用 ----------------

    def _start_guard(self):
        self._set_state("checking", "解析目标并读回工厂 EDID")
        tgt, why = self._same_target_now()
        if tgt is None:
            self._backoff_until = self._clock() + self.backoff
            return self._set_state("ambiguous" if "多" in why else "failed", why)
        self._target = tgt
        try:
            factory = self._dump(tgt["id"])
        except Exception as e:
            self._backoff_until = self._clock() + self.backoff
            self._last_error = repr(e)
            return self._set_state("failed", "读不到工厂 EDID: %s" % e)

        # Per-device full identity AND full factory hash, never one global backup.
        # A changed EDID for an already-known physical identity is not a new unit.
        try:
            validate(factory)
            if hashlib.sha256(factory).hexdigest() != tgt["factory_sha256"]:
                raise EDIDError("full factory identity changed during selection; refusing")
            profile = bytearray(factory)
            profile[12:18] = bytes(6)
            profile[127] = 0
            if len(factory) != 384 or hashlib.sha256(profile).hexdigest() != FACTORY_PROFILE_SHA256:
                raise EDIDError("unknown factory firmware / preexisting EDID override; refusing")
            identity = hashlib.sha256(factory[8:16] + TARGET_NAME.encode("ascii") +
                                      repr(TARGET_NATIVE).encode("ascii")).hexdigest()
            factory_hash = hashlib.sha256(factory).hexdigest()
            meta_path = os.path.join(self.state_dir, "edid_factory-" + identity + ".json")
            self._backup_file = os.path.join(self.state_dir,
                                            "edid_factory-" + identity + "-" + factory_hash + ".edid")
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("factory_sha256") != factory_hash:
                    raise EDIDError("当前 EDID 与同一完整身份的工厂备份不同 (override) → 拒绝覆盖")
            size = self._safe_size(tgt["id"])
            if not size or any(abs(v - cm * 10) > self.tol_mm
                               for v, cm in zip(size, factory[21:23])):
                raise EDIDError("有效几何与工厂 EDID 不符 / 不可读 (疑似 override) → 拒绝覆盖")
            bp = self.backup_path()
            if os.path.exists(bp):
                with open(bp, "rb") as f:
                    if f.read() != factory:
                        raise EDIDError("factory backup corrupted; refusing")
            else:
                with open(bp + ".tmp", "wb") as f:
                    f.write(factory)
                os.replace(bp + ".tmp", bp)
            meta = dict(tgt, factory_sha256=factory_hash, device_identity_sha256=identity,
                        saved_at=self._clock(), backup=bp)
            with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(meta_path + ".tmp", meta_path)
        except Exception as e:
            self._backoff_until = self._clock() + self.backoff
            self._last_error = str(e)
            return self._set_state("failed", "工厂备份/override 校验失败: %s" % e)

        patched = patch_geometry(bytes(factory), self.geom[0], self.geom[1])
        pf = os.path.join(self.state_dir, "edid_patched.edid")
        with open(pf, "wb") as f:
            f.write(patched)

        self._readback_before = self._safe_size(tgt["id"])
        argv = ["guard", "--display", str(tgt["id"]), "--edid", pf,
                "--vendor", str(TARGET_VENDOR), "--product", str(TARGET_MODEL),
                "--name", TARGET_NAME,
                "--native", "%dx%d" % TARGET_NATIVE, "--factory", self.backup_path()]
        try:
            self._proc = self._spawn(argv)
        except Exception as e:
            self._proc = None
            self._backoff_until = self._clock() + self.backoff
            self._last_error = repr(e)
            return self._set_state("failed", "拉起助手失败: %s" % e)

        line = self._readline(timeout=25)
        try:
            doc = json.loads(line or "{}")
            if not isinstance(doc, dict):
                doc = {"rc": 1, "error": "helper response is not a JSON object"}
        except Exception:
            doc = {}
        self._apply_report = doc
        self._restore_report = None
        if doc.get("rc") != 0 or doc.get("stage") != "applied":
            err = doc.get("error") or ("rc=%s" % doc.get("rc"))
            restoration = self._close_and_collect()
            self._backoff_until = self._clock() + self.backoff
            self._last_error = "%s; apply=%r; restore=%r" % (err, doc, restoration)
            return self._set_state("failed", "助手拒绝应用: %s" % err)

        # ★有效几何读回: API rc=0 不算成功, 必须真读回系统尺寸
        after = self._safe_size(tgt["id"])
        self._readback_after = after
        want = (self.geom[0], self.geom[1])
        if not after or abs(after[0] - want[0]) > self.tol_mm \
                or abs(after[1] - want[1]) > self.tol_mm:
            restoration = self._close_and_collect() or {}
            self._backoff_until = self._clock() + self.backoff
            restored = restoration.get("rc") == 0 and restoration.get("restored") and self._verify_restored()
            self._last_error = "readback %r; restore=%r" % (after, restoration)
            return self._set_state(
                "failed", "助手 rc=0 但有效几何读回 %r ≠ 期望 %r → %s" %
                (after, want, "已恢复" if restored else "未确认恢复"))
        self._sig = (self._displays_sig(), tgt["id"],
                     tuple(round(v, 1) for v in after))
        self._applied_at = self._clock()
        self._debounce_until = self._clock() + self.debounce
        self._last_error = ""
        return self._set_state(
            "applied", "已应用 %d×%d mm, 系统读回 %.1f×%.1f mm"
            % (want[0], want[1], after[0], after[1]))

    def _safe_size(self, did):
        try:
            return self._size(did)
        except Exception:
            return None

    def _readline(self, timeout=25.0):
        """A real subprocess is drained by a reader thread; partial lines cannot
        extend this caller's deadline. Fake test streams have no OS descriptors.
        """
        if self._proc is None:
            return ""
        q = getattr(self._proc, "_edid_lines", None)
        if q is not None:
            import queue
            try:
                return q.get(timeout=max(0.0, timeout))
            except queue.Empty:
                return ""
        try:
            return self._proc.stdout.readline()  # injected in-memory fixture only
        except Exception:
            return ""

    def _close_and_collect(self):
        """EOF requests original-handle restore; preserve process if it won't exit."""
        if self._proc is None:
            return None
        p = self._proc
        try:
            if p.stdin is not None:
                p.stdin.close()
        except Exception:
            pass
        line = self._readline(timeout=6)
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.terminate()  # SIGTERM handler requests restore, never SIGKILL
                p.wait(timeout=6)
            except Exception:
                return {"rc": 1, "stage": "restore-timeout", "restored": False,
                        "error": "guard still alive; retained, no reapply"}
        self._proc = None
        try:
            doc = json.loads(line or "{}")
            if not isinstance(doc, dict):
                doc = {"rc": 1, "error": "helper response is not a JSON object"}
        except Exception:
            doc = {}
        # On apply failure the first line may have been queued before restored.
        if doc.get("stage") != "restored":
            self._proc = p
            final = self._readline(timeout=.2)
            self._proc = None
            try:
                final_doc = json.loads(final or "{}")
                if final_doc.get("stage") == "restored":
                    doc = final_doc
            except Exception:
                pass
        doc["stderr"] = "".join(getattr(p, "_edid_stderr", ()))
        doc["exit_code"] = getattr(p, "returncode", None)
        self._restore_report = doc
        return doc

    def _on_guard_exit(self):
        """guard 进程自己退了 (EOF/信号/崩溃)。读它的恢复报告并验证。"""
        doc = self._close_and_collect() or {}
        self._backoff_until = self._clock() + self.backoff
        if doc.get("rc") != 0 or doc.get("stage") != "restored" or not doc.get("restored"):
            self._backoff_until = self._clock() + self.backoff
            self._last_error = "guard 退出且未确认恢复: %r" % doc
            return self._set_state("failed", "guard 异常退出, 未确认恢复: %s"
                                   % (doc.get("error") or "无报告"))
        if self._verify_restored():
            self._sig = None
            return self._set_state("disabled", "guard 已退出并确认恢复")
        return self._set_state("failed", "guard 报告恢复, 但读回验证不一致")

    def _restore(self, reason, verify=True):
        """主动关掉 guard: 助手经原保留句柄恢复工厂 EDID, 再读回验证。"""
        if self._proc is None:
            return self._set_state("disabled", reason)
        self.log("EDID 校正: 恢复工厂 EDID (%s)" % reason)
        doc = self._close_and_collect() or {}
        if doc.get("rc") != 0 or doc.get("stage") != "restored" or not doc.get("restored"):
            self._backoff_until = self._clock() + self.backoff
            self._last_error = "restore report: %r" % doc
            return self._set_state("failed", "助手未确认恢复: %s" % (doc.get("error") or doc))
        if verify and not self._verify_restored():
            self._backoff_until = self._clock() + self.backoff
            return self._set_state("failed", "恢复读回验证失败")
        self._sig = None
        self._readback_after = None
        return self._set_state("disabled", "%s → 已恢复工厂 EDID" % reason)

    def _verify_restored(self):
        """恢复后读回: 尺寸 ≈ 工厂基础块 cm, EDID 与备份逐字节一致。"""
        tgt = self._target
        bp = self.backup_path()
        if tgt is None or not os.path.exists(bp):
            return False
        with open(bp, "rb") as f:
            factory = f.read()
        ok_size = False
        size = self._safe_size(tgt["id"])
        cm = (factory[21], factory[22]) if len(factory) > 22 else (0, 0)
        if size and cm[0] and cm[1]:
            ok_size = abs(size[0] - cm[0] * 10) <= self.tol_mm \
                and abs(size[1] - cm[1] * 10) <= self.tol_mm
        ok_edid = False
        try:
            ok_edid = self._dump(tgt["id"]) == factory
        except Exception:
            ok_edid = False
        if not (ok_size and ok_edid):
            self._last_error = "restore verify: size=%s ok_size=%s ok_edid=%s" \
                % (size, ok_size, ok_edid)
            self.log("EDID 校正: 恢复读回验证失败 (%s)" % self._last_error)
            return False
        return True

    # ---------------- 退出 ----------------

    def shutdown(self, reason="App 退出"):
        """App 退出/重启前必须调用: 关 guard -> 助手经原句柄恢复 -> 读回验证。"""
        try:
            if self._proc is not None:
                return self._restore(reason)
            return self._set_state("disabled", reason)
        finally:
            self.status()
