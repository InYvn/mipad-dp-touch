#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EDID 几何修正 (B) 集成自检 —— 离线为主, 不碰真实显示器。

钉住五组行为 (与 src/dptouch_edid.py 一一对应):
  1. EDID 校验: 保留完整 384 字节 (multipes128 + extension-count + 每块校验和);
     截断 / 校验和坏 / 扩展数不符 / 头魔数坏 / 长度非 128 倍数 -> 全部拒绝。
  2. 几何补丁白名单: 只允许改「物理尺寸字段 + 各块校验和」, 其它字节一个都不许动;
     身份 (vendor/product/serial)、全部时序、DisplayID 扩展块逐字节不变;
     补丁结果与已实测归档的 mi-display-size-only.edid 逐字节一致。
  3. 目标屏解析: vendor 25001 × product 4 + EDID 名 "MI DISPLAY" + 原生时序
     3408x2272 + 排除自带屏 + 唯一匹配; 重复身份 / 名字不符 / 时序不符 /
     非目标厂商 / 无效 EDID 全部拒绝 —— 绝不猜、绝不写死序号或序列号。
  4. 控制器生命周期 (mock 助手): apply / 有效几何读回(rc 成功不算数) / 失败 /
     restore / 第三方残留 override 拒绝 / 拓扑变化只重应用一次 / 退避不每秒闪。
  5. 原生助手 CLI: 必须先编译; 缺失视为失败, 不静默跳过。

用法: python3 tools/selftest_edid.py            (全过 exit 0, 有失败 exit 1)
      python3 tools/selftest_edid.py --live-guard   (真屏一次性 apply->EOF恢复读回,
                                                     要求平板在线且当前无任何 override)
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import dptouch_edid as ED          # noqa: E402

EVID = os.environ.get("DPTOUCH_EDID_TEST_FIXTURES", os.path.join(ROOT, "tools", "fixtures", "private-device"))
ORIG = open(os.path.join(EVID, "mi-display-original.edid"), "rb").read()
GOLD = open(os.path.join(EVID, "mi-display-size-only.edid"), "rb").read()

FAILED = []
N = [0]


def check(name, got, want):
    N[0] += 1
    if got != want:
        FAILED.append((name, want, got))
        print("  ✗ %s\n     期望 %r\n     实际 %r" % (name, want, got))


def check_true(name, ok, detail=""):
    N[0] += 1
    if not ok:
        FAILED.append((name, True, False))
        print("  ✗ %s%s" % (name, " | " + detail if detail else ""))


def check_raises(name, fn, exc=Exception):
    N[0] += 1
    try:
        fn()
    except exc:
        return
    except Exception as e:          # 抛了但不是该抛的那种
        FAILED.append((name, "raise %s" % exc.__name__, "raise %r" % e))
        print("  ✗ %s: 抛了 %r, 期望 %s" % (name, e, exc.__name__))
        return
    FAILED.append((name, "raise %s" % exc.__name__, "没有抛"))
    print("  ✗ %s: 没有抛异常 (期望 %s)" % (name, exc.__name__))


# --------------------------------------------------------------------------
# 组 1: EDID 校验 / 解析
# --------------------------------------------------------------------------

def t_validate():
    print("== 组1: EDID 校验与解析 (384B / multiples128 / 扩展数 / 块校验和) ==")
    info = ED.info_of(ORIG)
    check("384B 通过", info is not None, True)
    check("扩展数=2 (3块)", info["blocks"], 3)
    check("vendor 25001", info["vendor"], 25001)
    check("product 4", info["model"], 4)
    check("EDID 名 MI DISPLAY", info["name"], "MI DISPLAY")
    check("原生时序 3408x2272", info["preferred"], (3408, 2272))
    check("基础块 cm 59x34", info["base_cm"], (59, 34))

    bad = bytearray(ORIG)
    bad[200] ^= 1                       # 第二块校验和坏
    check_raises("扩展块校验和坏 -> 拒", lambda: ED.info_of(bytes(bad)), ED.EDIDError)
    bad = bytearray(ORIG)
    bad[127] ^= 1                       # 基础块校验和坏
    check_raises("基础块校验和坏 -> 拒", lambda: ED.info_of(bytes(bad)), ED.EDIDError)
    check_raises("截断 383B -> 拒", lambda: ED.info_of(ORIG[:383]), ED.EDIDError)
    check_raises("长度非128倍数 -> 拒", lambda: ED.info_of(ORIG + b"\x00"), ED.EDIDError)
    bad = bytearray(ORIG)
    bad[0] ^= 1
    check_raises("头魔数坏 -> 拒", lambda: ED.info_of(bytes(bad)), ED.EDIDError)
    bad = bytearray(ORIG)
    bad[126] = 1                        # 扩展数与实际不符
    bad[127] = (-sum(bad[0:127])) & 255
    check_raises("扩展数不符 -> 拒", lambda: ED.info_of(bytes(bad)), ED.EDIDError)
    check_raises("空 EDID -> 拒", lambda: ED.info_of(b""), ED.EDIDError)


# --------------------------------------------------------------------------
# 组 2: 几何补丁白名单
# --------------------------------------------------------------------------

def t_patch():
    print("== 组2: 几何补丁白名单 (只改尺寸字段+校验和, 与归档 B 逐字节一致) ==")
    after = ED.patch_geometry(ORIG, 282, 188)
    check("补丁 == 归档 mi-display-size-only.edid", after, GOLD)
    check("基础块 cm = 28x19", after[21:23], bytes([28, 19]))
    info = ED.info_of(after)
    check("补丁后仍通过校验", info["blocks"], 3)
    check("补丁后名字不变", info["name"], "MI DISPLAY")
    check("补丁后时序不变", info["preferred"], (3408, 2272))
    # 白名单: 允许改的 = {21,22} ∪ 每个真实 DTD 的 +12/+13/+14 ∪ 各块校验和
    ok, bad = ED.geometry_only(ORIG, after)
    diff = {i for i, (a, b) in enumerate(zip(ORIG, after)) if a != b}
    check_true("确实改了东西", bool(diff))
    check_true("改动全部落在白名单内", ok and not bad,
               "违规字节: %s (总改动 %d)" % (sorted(bad)[:10], len(diff)))
    # 硬断言 (即使白名单逻辑有洞, 这些也不能动):
    check("身份 vendor/product 不变", after[8:12], ORIG[8:12])
    check("序列号不变", after[12:16], ORIG[12:16])
    check("首选时序 3408x2272 不变", after[54:66], ORIG[54:66])
    check("DisplayID 扩展块逐字节不变", after[128:256], ORIG[128:256])
    check("CTA-861 块头部不变", after[256:262], ORIG[256:262])
    # 再打一次同参数 = 不动 (幂等, 不会反复触发显示器重配置)
    check("幂等: 再补丁不变", ED.patch_geometry(after, 282, 188), after)
    cta0 = bytearray(ORIG)
    cta0[258] = 0  # Legal CTA: no DTD collection.
    cta0[383] = (-sum(cta0[256:383])) & 255
    check_true("CTA start=0 accepts no DTD collection",
               ED.geometry_only(bytes(cta0), ED.patch_geometry(bytes(cta0), 282, 188))[0])
    check_raises("geometry_only rejects invalid candidate checksum",
                 lambda: ED.geometry_only(ORIG, GOLD[:-1] + bytes([GOLD[-1] ^ 1])), ED.EDIDError)
    check_raises("尺寸 0 -> 拒", lambda: ED.patch_geometry(ORIG, 0, 188), ED.EDIDError)
    check_raises("尺寸 3000 -> 拒", lambda: ED.patch_geometry(ORIG, 282, 3000), ED.EDIDError)
    check_raises("尺寸非整数 -> 拒", lambda: ED.patch_geometry(ORIG, 282.5, 188), ED.EDIDError)
    check_raises("无效 EDID 不补丁", lambda: ED.patch_geometry(ORIG[:200], 282, 188), ED.EDIDError)


# --------------------------------------------------------------------------
# 组 3: 目标屏解析 (多屏安全)
# --------------------------------------------------------------------------

def _disp(did, vendor=25001, model=4, builtin=False):
    return {"id": did, "vendor": vendor, "model": model, "builtin": builtin}


def t_target():
    print("== 组3: 目标屏解析 (身份+名字+时序, 唯一匹配, 排除自带屏) ==")
    tgt, why = ED.find_target([_disp(2)], lambda did: ORIG)
    check("唯一匹配 -> 命中", bool(tgt), True)
    if tgt:
        check("目标 id", tgt["id"], 2)
        check("目标名字", tgt["name"], "MI DISPLAY")
    check("原因为空", why, "")
    tgt, why = ED.find_target([_disp(2), _disp(7)], lambda did: ORIG)
    check("重复身份 -> 拒绝", tgt is None, True)
    check_true("重复身份原因里说'多'", "多" in why or "不猜" in why, why)
    tgt, why = ED.find_target([_disp(1, builtin=True)], lambda did: ORIG)
    check("只有自带屏 -> 拒绝", tgt is None, True)
    tgt, why = ED.find_target([_disp(2), _disp(1, builtin=True)], lambda did: ORIG)
    check("自带屏同身份不参与 -> 命中", bool(tgt) and tgt["id"] == 2, True)
    tgt, why = ED.find_target([_disp(2, vendor=0x05AC)], lambda did: ORIG)
    check("非目标厂商 -> 拒绝", tgt is None, True)
    tgt, why = ED.find_target([_disp(2)], lambda did: ORIG[:128])
    check("只有 128B (非 384) -> 拒绝", tgt is None, True)
    # 名字不符: 把描述符里的 MI DISPLAY 换成别的 (改在名字描述符内, 校验和重算)
    renamed = bytearray(ORIG)
    for i in range(110, 121):
        renamed[i] = 0x41           # "AAAAAAAAAAA"
    renamed[127] = (-sum(renamed[0:127])) & 255
    tgt, why = ED.find_target([_disp(2)], lambda did: bytes(renamed))
    check("EDID 名不符 -> 拒绝", tgt is None, True)
    check_true("原因提到名字", "名" in why or "MI DISPLAY" in why, why)
    # 时序不符: 首选 DTD 的水平活动改成别的
    retimed = bytearray(ORIG)
    retimed[54 + 2] = 0x20          # H active LSB 改掉
    retimed[127] = (-sum(retimed[0:127])) & 255
    tgt, why = ED.find_target([_disp(2)], lambda did: bytes(retimed))
    check("原生时序不符 -> 拒绝", tgt is None, True)
    tgt, why = ED.find_target([_disp(2)], lambda did: (_ for _ in ()).throw(OSError("dump failed")))
    check("EDID 读不出 -> 拒绝(不崩)", tgt is None, True)
    check("不写死序列号/枚举序号", "33554434" not in ED.__file__ and "index2" not in
          open(ED.__file__, encoding="utf-8").read().lower().replace(" ", ""), True)


# --------------------------------------------------------------------------
# 组 4: 控制器生命周期 (mock 助手)
# --------------------------------------------------------------------------

class _FakePipe(object):
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.proc.on_stdin_closed()


class _FakeStdout(object):
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


class FakeProc(object):
    """模拟 vedid guard 子进程: stdin 关闭 -> 附加 restored 行并退出。"""

    def __init__(self, spawn_ctx, lines, rc=0):
        self.ctx = spawn_ctx
        self.rc = rc
        self.terminated = False
        self.stdout = _FakeStdout(lines)
        self.stdin = _FakePipe(self)

    def poll(self):
        return self.rc if self.stdin.closed else None

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        self.terminated = True

    def on_stdin_closed(self):
        self.ctx.on_stdin_closed(self)


class FakeEnv(object):
    """可编程的助手环境: 记录 spawn 调用, 按脚本返回 JSON 行。"""

    def __init__(self, flip=True):
        self.calls = []              # 每次 spawn 的 argv
        self.procs = []
        self.size = (588.9, 339.5)   # 当前 CGDisplayScreenSize (可被脚本翻转)
        self.factory = ORIG           # 当前 AV 服务读到的 EDID
        self.next_lines = []          # 下一次 spawn 时 stdout 的第一行
        self.flip = flip              # spawn 后是否把 size 翻到修正值 (模拟助手真应用了)

    def spawn(self, argv):
        self.calls.append(list(argv))
        lines = list(self.next_lines) or [
            json.dumps({"cmd": "guard", "stage": "applied", "rc": 0,
                        "sizeBefore": {"w": 588.9, "h": 339.5},
                        "sizeAfter": {"w": 280.1, "h": 189.8}})]
        p = FakeProc(self, lines)
        self.procs.append(p)
        if self.flip:
            self.size = (280.1, 189.8)   # 助手真应用了: 系统读回变成修正值
        return p

    def on_stdin_closed(self, proc):
        self.size = (588.9, 339.5)   # 助手恢复了工厂几何
        proc.stdout.lines.append(json.dumps(
            {"cmd": "guard", "stage": "restored", "rc": 0, "restored": True,
             "sizeAfterReset": {"w": 588.9, "h": 339.5}}))


def _mk_ctl(env, **kw):
    kw.setdefault("helper", "/usr/bin/true")   # 隔离: 不依赖本机有没有编译出 vedid
    kw.setdefault("displays", lambda: [_disp(2)])
    kw.setdefault("dump", lambda did: env.factory)
    kw.setdefault("size", lambda did: env.size)
    kw.setdefault("spawn", env.spawn)
    kw.setdefault("clock", time.time)
    kw.setdefault("debounce", 0.0)
    kw.setdefault("backoff", 0.0)
    kw.setdefault("state_dir", tempfile.mkdtemp(prefix="dptouch-edid-test-"))
    logs = []
    kw.setdefault("log", logs.append)
    ctl = ED.EDIDController(**kw)
    ctl._helper_ok = True  # explicitly mocked only here; process tests use real probes
    return ctl, logs


def t_controller():
    print("== 组4: 控制器生命周期 (mock 助手: apply/读回/失败/恢复/第三方override/拓扑) ==")
    # --- 4.1 正常 apply + 有效几何读回 (rc 成功不算数, 必须真读回) ---
    env = FakeEnv()
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("apply 后状态 applied", st["state"], "applied")
    check("恰好 spawn 一次", len(env.calls), 1)
    check("spawn 带 guard 子命令", env.calls[0][0], "guard")
    argv = " ".join(env.calls[0])
    check_true("带 --display 身份", "--display 2" in argv, argv)
    check_true("带 --name MI DISPLAY", "MI DISPLAY" in argv, argv)
    check_true("带 --native 3408x2272", "3408x2272" in argv, argv)
    check("读回尺寸已记录", st["readback_after"], (280.1, 189.8))
    check_true("工厂备份已落盘", os.path.exists(ctl.backup_path())
               and open(ctl.backup_path(), "rb").read() == ORIG)
    check_true("会话 JSON 状态已落盘", os.path.exists(os.path.join(ctl.state_dir,
                                                                  "edid_status.json")))
    # 连续 tick 不再 spawn (不允许每秒闪/反复 apply)
    for _ in range(5):
        st = ctl.tick()
    check("连续 5 拍后仍只 spawn 一次", len(env.calls), 1)
    check("状态保持 applied", st["state"], "applied")

    # --- 4.2 有效几何读回失败: API rc 成功但尺寸没变 -> 必须判失败并恢复 ---
    env = FakeEnv(flip=False)         # 助手没真应用: 尺寸保持不变
    env.next_lines = [json.dumps({"cmd": "guard", "stage": "applied", "rc": 0,
                                  "sizeBefore": {"w": 588.9, "h": 339.5},
                                  "sizeAfter": {"w": 588.9, "h": 339.5}})]  # 没生效
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("读回没变 -> failed", st["state"], "failed")
    check_true("stdin 已关闭(触发恢复)", bool(env.procs) and env.procs[0].stdin.closed)
    check_true("失败原因写进状态", "读回" in st.get("detail", "") or "尺寸" in st.get("detail", ""),
               st.get("detail"))

    # --- 4.3 助手 API rc != 0 -> failed, 不冒充成功 ---
    env = FakeEnv()
    env.next_lines = [json.dumps({"cmd": "guard", "stage": "applied", "rc": 5,
                                  "error": "no AV service"})]
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("API rc!=0 -> failed", st["state"], "failed")
    check_true("detail 透出助手错误", "no AV service" in st.get("detail", ""), st.get("detail"))

    # --- 4.4 关掉校正 -> 恢复, 且恢复后几何要读回验证 ---
    env = FakeEnv()
    ctl, logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    ctl.tick()
    env.size = (280.1, 189.8)          # apply 后的真实状态
    ctl.set_desired(True, True, False)
    st = ctl.tick()
    check("关掉校正 -> disabled", st["state"], "disabled")
    check_true("guard 的 stdin 已关", bool(env.procs) and env.procs[0].stdin.closed)
    check("恢复后尺寸回到工厂值", env.size, (588.9, 339.5))
    check_true("日志提到恢复", any("恢复" in m or "restored" in m or "还原" in m for m in logs),
               str(logs[-3:]))

    # --- 4.5 第三方残留 override: 备份存在但当前 EDID 变了 -> 拒绝覆盖 ---
    env = FakeEnv()
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    ctl.tick()                          # 第一次成功, 备份落盘
    env.procs.clear()
    env.factory = GOLD                  # 有个"别的" override 在生效
    env.calls.clear()
    ctl.set_desired(False, False, False)
    ctl.tick()                          # 先退干净
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("第三方 override -> failed 拒绝", st["state"], "failed")
    check_true("detail 提到 override/不是工厂",
               "override" in st.get("detail", "").lower() or "不是" in st.get("detail", ""),
               st.get("detail"))
    check("不覆盖: 没有新 spawn", len(env.calls), 0)

    # --- 4.6 首次启用就发现尺寸与 EDID 声明的 cm 对不上 -> 视为已有 override, 拒绝 ---
    env = FakeEnv()
    env.size = (280.0, 190.0)          # 系统读回 ≠ 工厂 59x34cm
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("首次启用即发现 override -> failed", st["state"], "failed")
    check("无 spawn (没动过显示器)", len(env.calls), 0)

    # --- 4.7 拓扑变化: 目标屏换 id -> 旧 guard 关掉, 只重应用一次 ---
    env = FakeEnv()
    infos = [_disp(2)]
    ctl, _logs = _mk_ctl(env, displays=lambda: infos)
    ctl.set_desired(True, True, True)
    ctl.tick()
    check("初始 applied", ctl.status()["state"], "applied")
    env.size = (588.9, 339.5)          # 拔插: override 掉了
    infos[0] = _disp(9)                # 重插后 displayID 变了
    ctl.tick()
    st = ctl.tick()
    check("拓扑变化后重应用 -> applied", st["state"], "applied")
    check("共 spawn 2 次 (旧恢复+新应用)", len(env.calls), 2)
    check("第二次指向新 id", "--display 9" in " ".join(env.calls[-1]), True)
    for _ in range(4):
        ctl.tick()
    check("稳定后不再 spawn", len(env.calls), 2)

    # --- 4.8 拓扑变化后变成多块 -> 拒绝且退避 (不反复试) ---
    env = FakeEnv()
    infos = [_disp(2)]
    clock = [1000.0]
    ctl, _logs = _mk_ctl(env, displays=lambda: infos, clock=lambda: clock[0],
                         debounce=0.0, backoff=30.0)
    ctl.set_desired(True, True, True)
    ctl.tick()
    infos[:] = [_disp(2), _disp(9)]    # 第二块同身份屏出现
    clock[0] += 5
    ctl.tick()
    st = ctl.tick()
    check("多块 -> ambiguous", st["state"], "ambiguous")
    n = len(env.calls)
    for _ in range(3):
        clock[0] += 1
        ctl.tick()
    check("退避期内不再试", len(env.calls), n)

    # --- 4.9 失败后退避: 不再每秒重试, 到点才允许再来一次 ---
    env = FakeEnv()
    env.next_lines = [json.dumps({"cmd": "guard", "stage": "applied", "rc": 5,
                                  "error": "boom"})]
    clock = [2000.0]
    ctl, _logs = _mk_ctl(env, clock=lambda: clock[0], debounce=0.0, backoff=15.0)
    ctl.set_desired(True, True, True)
    ctl.tick()
    n = len(env.calls)
    check("第一次失败", ctl.status()["state"], "failed")
    for _ in range(3):
        clock[0] += 1
        ctl.tick()
    check("退避期内不再试", len(env.calls), n)
    clock[0] += 20
    ctl.tick()
    check("退避到期后重试一次", len(env.calls), n + 1)

    # --- 4.10 未配置 (enabled/native/correct 任一为否) -> 不管理 ---
    env = FakeEnv()
    ctl, _logs = _mk_ctl(env)
    for flags in ((False, True, True), (True, False, True), (True, True, False)):
        ctl.set_desired(*flags)
        st = ctl.tick()
        check("未配置 %r -> disabled" % (flags,), st["state"], "disabled")
    check("从未 spawn", len(env.calls), 0)

    # --- 4.11 助手缺失 -> unsupported, 特性安全禁用 ---
    ctl = ED.EDIDController(state_dir=tempfile.mkdtemp(prefix="dptouch-edid-test-"),
                            helper="/nonexistent/vedid", log=lambda m: None,
                            displays=lambda: [_disp(2)],
                            dump=lambda did: ORIG,
                            size=lambda did: (588.9, 339.5))
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check("无助手 -> unsupported", st["state"], "unsupported")
    check("helper_ok=False", st["helper_ok"], False)

    # Different physical unit must have its own identity/hash-keyed backup.
    env = FakeEnv()
    ctl, _ = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    ctl.tick(); first_path = ctl.backup_path(); ctl.shutdown()
    another = bytearray(ORIG); another[12] ^= 1
    another[127] = (-sum(another[:127])) & 255
    env.factory = bytes(another)
    st = ctl.tick()
    check("different unit may apply in same support directory", st["state"], "applied")
    check_true("different full identity has separate factory backup", ctl.backup_path() != first_path)
    ctl.shutdown()

    # A helper saying restore failed must not be hidden by coincidentally clean CG size.
    env = FakeEnv()
    ctl, _ = _mk_ctl(env); ctl.set_desired(True, True, True); ctl.tick()
    original_close = env.on_stdin_closed
    def failed_restore(p):
        original_close(p)
        p.stdout.lines[-1] = json.dumps({"stage":"restored", "rc":1,
                                       "restored":False, "error":"reset failed witness"})
    env.on_stdin_closed = failed_restore
    check("native restore failure cannot report disabled", ctl.shutdown()["state"], "failed")

    # Reject a device changing between target selection and factory backup.
    env = FakeEnv()
    changed = bytearray(ORIG); changed[12] ^= 1; changed[127] = (-sum(changed[:127])) & 255
    dumps = [0]
    def changing_dump(did):
        dumps[0] += 1
        return ORIG if dumps[0] == 1 else bytes(changed)
    ctl, _ = _mk_ctl(env, dump=changing_dump)
    ctl.set_desired(True, True, True)
    check("selection/factory full identity race is rejected", ctl.tick()["state"], "failed")
    check("identity race never spawns writer", len(env.calls), 0)

    env = FakeEnv()
    infos = [_disp(2)]
    ctl, _ = _mk_ctl(env, displays=lambda: infos)
    ctl.set_desired(True, True, True); ctl.tick()
    infos.clear(); env.size = None
    check("disconnection detected even when geometry unavailable", ctl._topology_changed(), True)
    ctl.shutdown()

    env = FakeEnv(flip=False)
    ctl, _ = _mk_ctl(env)
    def failed_readback_restore(proc):
        proc.stdout.lines.append(json.dumps({"stage":"restored", "rc":1,
                                             "restored":False, "error":"restore witness"}))
    env.on_stdin_closed = failed_readback_restore
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    check_true("apply readback failure preserves failed restore diagnostic",
               "restore witness" in st["last_error"] and "未确认" in st["detail"], str(st))

    # First launch must not trust an unknown timing/extension override merely
    # because physical geometry still looks factory-clean.
    env = FakeEnv()
    unknown = bytearray(ORIG); unknown[150] ^= 1
    unknown[255] = (-sum(unknown[128:255])) & 255
    env.factory = bytes(unknown)
    ctl, _ = _mk_ctl(env); ctl.set_desired(True, True, True)
    check("unknown firmware/override rejected before first backup", ctl.tick()["state"], "failed")
    check("unknown factory never spawns writer", len(env.calls), 0)

    env = FakeEnv(); env.next_lines = ["[]"]
    ctl, _ = _mk_ctl(env); ctl.set_desired(True, True, True)
    try:
        st = ctl.tick()
        protocol_safe = st["state"] == "failed" and env.procs[0].stdin.closed
    except Exception:
        protocol_safe = False
    check("non-object JSON response fails closed and releases guard", protocol_safe, True)

    # --- 4.12 shutdown: 进程退出前必须关掉 guard (恢复) ---
    env = FakeEnv()
    ctl, _logs = _mk_ctl(env)
    ctl.set_desired(True, True, True)
    ctl.tick()
    st = ctl.shutdown("app quit")
    check("shutdown 后 disabled", st["state"], "disabled")
    check_true("shutdown 已关 guard stdin", env.procs[0].stdin.closed)
    check("恢复后尺寸为工厂值", env.size, (588.9, 339.5))


# --------------------------------------------------------------------------
# 组 5: 原生助手 CLI (二进制存在才跑)
# --------------------------------------------------------------------------

def t_process_io():
    """Real harmless subprocesses, not fake streams; never invokes a display writer."""
    import subprocess
    ctl = ED.EDIDController(tempfile.mkdtemp(prefix="edid-io-"), helper=sys.executable)
    ctl._proc = ctl._spawn(["-c", "import sys,time;sys.stdout.write('{');sys.stdout.flush();time.sleep(.7)"])
    started = time.monotonic()
    line = ctl._readline(timeout=.1)
    check_true("partial JSON cannot defeat deadline", time.monotonic()-started < .4)
    check("partial JSON timeout returns no line", line, "")
    ctl._proc.wait(timeout=2); ctl._proc = None
    ctl._proc = ctl._spawn(["-c", "import sys;sys.stderr.write('E'*262144);sys.stderr.flush();print('{\"rc\":0}')"])
    line = ctl._readline(timeout=1)
    check("stderr flood cannot deadlock stdout protocol", line.strip(), '{"rc":0}')
    ctl._proc.terminate(); ctl._proc.wait(timeout=2); ctl._proc = None
    ctl.helper = "/usr/bin/true"; ctl._helper_ok = None
    check("exit zero without helper selftest JSON is not supported", ctl.helper_ok(), False)


def t_helper_cli():
    print("== 组5: 原生助手 CLI (必须存在; 离线边界/validate/selftest) ==")
    helper = ED.find_helper()
    if not helper:
        check_true("原生助手必须已编译 (make -C tools/native)", False)
        return
    import subprocess
    r = subprocess.run([helper, "validate", "--edid",
                        os.path.join(EVID, "mi-display-original.edid")],
                       capture_output=True, text=True, timeout=30)
    out = {}
    try:
        out = json.loads(r.stdout)
    except Exception:
        pass
    check("vedid validate 384B 通过", (r.returncode, out.get("valid")), (0, True))
    r = subprocess.run([helper, "validate", "--edid",
                        os.path.join(EVID, "mi-display-size-only.edid")],
                       capture_output=True, text=True, timeout=30)
    check("vedid validate B 版 384B 通过", r.returncode, 0)
    bad = os.path.join(tempfile.mkdtemp(prefix="dptouch-edid-"), "bad.edid")
    with open(bad, "wb") as f:
        f.write(ORIG[:383])
    r = subprocess.run([helper, "validate", "--edid", bad],
                       capture_output=True, text=True, timeout=30)
    check("vedid validate 截断 -> 拒", r.returncode, 1)
    def patch_check(before, after):
        td = tempfile.mkdtemp(prefix="native-boundary-")
        a, b = os.path.join(td, "factory"), os.path.join(td, "candidate")
        open(a, "wb").write(before)
        open(b, "wb").write(after)
        return subprocess.run([helper, "check-patch", "--factory", a, "--edid", b],
                              capture_output=True, text=True, timeout=5).returncode
    check("native boundary accepts exact B", patch_check(ORIG, GOLD), 0)
    bad_id = bytearray(GOLD)
    bad_id[12] ^= 1
    bad_id[127] = (-sum(bad_id[:127])) & 255
    check("native boundary rejects changed identity", patch_check(ORIG, bytes(bad_id)), 1)
    check("native boundary rejects alternate geometry", patch_check(ORIG, ED.patch_geometry(ORIG, 300, 200)), 1)
    check("native boundary rejects unchanged factory", patch_check(ORIG, ORIG), 1)
    def factory_check(data):
        td = tempfile.mkdtemp(prefix="native-factory-")
        f = os.path.join(td, "factory.edid"); open(f,"wb").write(data)
        return subprocess.run([helper,"check-factory","--factory",f],
                              capture_output=True,text=True,timeout=5).returncode
    check("native known factory profile accepted",factory_check(ORIG),0)
    serial_unit = bytearray(ORIG); serial_unit[12] ^= 1
    serial_unit[127] = (-sum(serial_unit[:127])) & 255
    check("native factory profile is not tied to serial",factory_check(bytes(serial_unit)),0)
    timing_override = bytearray(ORIG); timing_override[56] ^= 1
    timing_override[127] = (-sum(timing_override[:127])) & 255
    check("unknown timing override with factory geometry rejected",factory_check(bytes(timing_override)),1)
    check("native B cannot masquerade as clean factory",factory_check(GOLD),1)
    native = open(os.path.join(HERE, "native", "vedid.m")).read()
    check_true("native rejects unguarded apply/reset entrypoints",
               "unguarded writes are disabled" in native)
    check_true("native guard enforces boundary check", "CheckPatch(factory, patched)" in native)
    check_true("native mapping has no sticky subtree state", "inSubtree" not in native)
    r = subprocess.run([helper, "snapshot"], capture_output=True, text=True, timeout=5)
    snapshot = json.loads(r.stdout)
    check_true("readonly snapshots include every screen effective geometry",
               r.returncode == 0 and all("sizeMM" in d for d in snapshot["displays"]))
    r = subprocess.run([helper, "dump", "--display", "4294967298", "--out", "-"],
                       capture_output=True, text=True, timeout=5)
    check("overflow display selector must not alias display 2", r.returncode, 1)
    r = subprocess.run([helper, "selftest"], capture_output=True, text=True, timeout=30)
    check("vedid selftest 通过", r.returncode, 0)
    t_fresh_geometry_reader()


# --------------------------------------------------------------------------
# 组 6: 一次性真机 guard 自检 (--live-guard; 默认不跑)
#   应用 -> 有效几何读回 -> 关 stdin (父进程 EOF) -> 助手经保留句柄恢复 -> 再读回验证。
#   全程 RAM-only, 结束必然回到工厂 EDID; 要求平板在线且当前没有任何 override。
# --------------------------------------------------------------------------

def t_fresh_geometry_reader():
    from unittest.mock import patch
    response={"rc":0,"stdout":json.dumps({"id":2,"rc":0,"sizeMM":{"w":280.1,"h":189.8}}),"stderr":""}
    with tempfile.TemporaryDirectory() as td, patch.object(ED,"_run_helper",return_value=response) as query:
        ctl=ED.EDIDController(state_dir=td,helper="/test/vedid")
        check("controller independent fresh geometry",ctl._size(2),(280.1,189.8))
        check("fresh query was independent helper",query.called,True)


def t_live_guard():
    helper = ED.find_helper()
    if not helper:
        print("  ✗ 没有 vedid 二进制"); return False
    import subprocess
    tgt, why = ED.find_target(ED.default_displays(), lambda did: ED.default_dump(helper, did))
    if tgt is None:
        print("  ✗ 找不到目标屏: %s" % why); return False
    before = ED.default_size(tgt["id"])
    print("  目标: id=%s %s | 工厂几何读回 %.1f×%.1f mm"
          % (tgt["id"], tgt["name"], before[0], before[1]))
    # 已有 override 时拒绝 (自检不允许覆盖第三方状态)
    if abs(before[0] - ED.GEO_MM[0]) < 10 and abs(before[1] - ED.GEO_MM[1]) < 10:
        print("  ✗ 当前几何已经是修正值 (疑似残留 override) -> 拒绝自检"); return False
    ctl = ED.EDIDController(state_dir=tempfile.mkdtemp(prefix="dptouch-liveguard-"),
                            helper=helper, log=lambda m: print("    [ctl] " + m))
    ctl.set_desired(True, True, True)
    st = ctl.tick()
    ok1 = st["state"] == "applied"
    print("  apply+读回: state=%s readback=%s" % (st["state"], st.get("readback_after")))
    time.sleep(0.5)
    st = ctl.shutdown("selftest EOF")
    after = ED.default_size(tgt["id"])
    ok2 = st["state"] == "disabled" and abs(after[0] - before[0]) < 3 \
        and abs(after[1] - before[1]) < 3
    print("  恢复+读回: state=%s | 工厂 %.1f×%.1f -> 现在 %.1f×%.1f mm | %s"
          % (st["state"], before[0], before[1], after[0], after[1],
             "通过" if ok2 else "失败"))
    # 独立双检: AV 服务读到的 EDID 与工厂备份逐字节一致
    try:
        dumped = ED.default_dump(helper, tgt["id"])
        same = dumped == ORIG
        print("  EDID 逐字节恢复: %s" % ("通过" if same else "失败"))
        ok2 = ok2 and same
    except Exception as e:
        print("  EDID 双检读不出: %r" % (e,))
    return ok1 and ok2


def main():
    print("EDID 几何修正 (B) 集成自检")
    for fn in (t_validate, t_patch, t_target, t_controller, t_process_io, t_helper_cli):
        try:
            fn()
        except Exception as e:
            import traceback
            print("  !! 组内异常: %r\n%s" % (e, traceback.format_exc()))
            FAILED.append((fn.__name__, "不抛异常", repr(e)))
    if "--live-guard" in sys.argv:
        ok = t_live_guard()
        if not ok:
            FAILED.append(("live-guard", True, False))
    print("共 %d 项断言, 失败 %d" % (N[0], len(FAILED)))
    for name, want, got in FAILED:
        print("  ✗ %s | 期望 %r 实际 %r" % (name, want, got))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
