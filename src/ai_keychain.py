#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Provider 的密钥存取（macOS 钥匙串）。

走 /usr/bin/security 的通用密码项，和 dptouch_unlock.py 同一套路：
* 服务名固定 `MipadDPTouch-ai`，账户 = provider id；
* 不写 config.json、不写日志、不写插件目录；
* 读出后只在进程内存里用，错误信息一律脱敏。

`security` 只有在**第一次**写入时可能弹钥匙串授权；之后的读取用的是
同一条 ACL（和便利解锁同样的行为）。
"""

import re
import subprocess

SERVICE = "MipadDPTouch-ai"
SECURITY = "/usr/bin/security"

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"          # OpenAI 风格
    r"|AIza[0-9A-Za-z_\-]{20,}"        # Google
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}"   # 通用
    r"|or-[A-Za-z0-9_\-]{20,})")       # OpenRouter


def redact(text):
    """把疑似密钥打码；日志/错误弹窗只准走这里。"""
    if not text:
        return text
    return _SECRET_RE.sub("[已隐藏]", str(text))


def _run(args, timeout=15):
    try:
        p = subprocess.run([SECURITY] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", repr(e)


def save(provider, key):
    """写入/更新；失败抛 RuntimeError（消息已脱敏）。"""
    provider = str(provider)
    if not provider or not key:
        raise RuntimeError("provider / key 不能为空")
    _run(["delete-generic-password", "-s", SERVICE, "-a", provider])
    rc, _out, err = _run(["add-generic-password", "-a", provider,
                          "-s", SERVICE, "-w", key, "-U"])
    if rc != 0:
        raise RuntimeError("写入钥匙串失败：%s" % redact(err or "security rc=%d" % rc))


def load(provider):
    """读密钥；没有/失败返回 None（不抛）。"""
    rc, out, _err = _run(["find-generic-password", "-w",
                          "-s", SERVICE, "-a", str(provider)])
    return out if rc == 0 and out else None


def has(provider):
    return load(provider) is not None


def delete(provider):
    _run(["delete-generic-password", "-s", SERVICE, "-a", str(provider)])


# ---------------------------------------------------------------- 离线自检

def selftest_offline():
    ok = 0
    bad = []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    ck("redact sk", "sk-" not in redact("key=sk-abcdefghijklmnop"))
    ck("redact google", "AIza" not in redact("x AIzaSyABCDEFGHIJKLMNOPQRSTUV 1"))
    ck("redact bearer", "Bearer" not in redact("Authorization: Bearer abcdefghijkl"))
    ck("redact 普通文本不动", redact("模型超时") == "模型超时")
    ck("空值安全", redact("") == "" and redact(None) is None)

    # 不碰真实钥匙串：只验证空参数会被挡
    try:
        save("", "x")
        ck("空 provider 被拒", False)
    except RuntimeError:
        ck("空 provider 被拒", True)
    try:
        save("openai", "")
        ck("空 key 被拒", False)
    except RuntimeError:
        ck("空 key 被拒", True)

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest_offline(), ensure_ascii=False))
