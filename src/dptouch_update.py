#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查更新 / 下载 / 校验 / 换包 —— 更新这件事的全部逻辑。

为什么不用 Sparkle
------------------
Sparkle 官方支持的路子假设你有 Developer ID 签名、一个 appcast.xml 和一对 EdDSA 密钥；
本 App 是 ad-hoc 签名（没有付费开发者账号），还要把 Sparkle 的 framework 和 XPC 服务塞进
PyInstaller 出来的 bundle 里。对「一个 14 MB 的 DMG、一个下载地址」这种规模，自己写这一层
更省事，也能把每一步都离线钉死。

这一层刻意不 import AppKit：版本号比较、从发版正文里抓 SHA256、生成更新脚本，全是纯函数，
所以 tools/selftest_update.py 能离线跑；真正碰网络的那两个函数接受注入的 opener，
测试时喂假数据即可。

更新流程（装机版）
------------------
1. check()   读 https://api.github.com/repos/<repo>/releases/latest，比版本号
2. download() 把 DMG 拿到本地，边下边算 SHA256，与发版正文里写的值比
3. write_installer() 写出一个小脚本：等本进程退出 -> 挂 DMG -> 拷新 app 到旁边 ->
   验签名 -> 旧包改名 -> 新包就位 -> 重新打开；任何一步失败都回滚
4. App 立刻硬退出（os._exit），把舞台交给脚本

这样一来「更新」不需要用户碰 Finder，也不会被 Gatekeeper 拦：包是我们自己下的，
Info.plist 里没有 LSFileQuarantineEnabled，系统不会给它打隔离属性。
"""

import hashlib
import json
import os
import re
import shlex
import tempfile
import urllib.error
import urllib.request

REPO = "InYvn/mipad-dp-touch"
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO

API_TIMEOUT = 15
DL_TIMEOUT = 900
UA = "MipadDPTouch"                       # GitHub API 对没有 UA 的请求直接 403
CHUNK = 1 << 16

# 更新脚本的日志 —— 与 App 的日志分开: 换包那一刻 App 已经退出, 出问题只能靠这个查
UPDATE_LOG = os.path.expanduser("~/Library/Logs/MipadDPTouch-update.log")


class UpdateError(Exception):
    """带着「能讲给用户听」的理由的失败。"""


# --------------------------------------------------------------------------
# 纯函数(离线可测)
# --------------------------------------------------------------------------

def parse_version(s):
    """'v1.1.0' -> (1, 1, 0); 预发布/构建后缀丢掉 ('1.1.0-beta.2' -> (1, 1, 0))。

    只认主.次.修订三段里的数字: 版本号是给人看的, 不要因为对方多写了个 'v'
    或者后缀就判定「没更新」。
    """
    s = str(s or "").strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote, local):
    """远端版本是否比本地新。比不出来(格式不认识)就当没有新版本 —— 宁可不动。"""
    r, l = parse_version(remote), parse_version(local)
    if not r or not l:
        return False
    n = max(len(r), len(l))
    r = r + (0,) * (n - len(r))
    l = l + (0,) * (n - len(l))
    return r > l


def parse_sha256(text):
    """从发版正文里抓校验值。

    正文里写的是 `SHA256  1f3a84d1…` 这种形式(Makefile/脚本都在用), 大小写、空格、
    反引号都容忍。抓不到就返回 None —— 调用方据此决定「没法校验」。
    """
    if not text:
        return None
    m = re.search(r"SHA256[^\n]*?([0-9a-fA-F]{64})", text)
    return m.group(1).lower() if m else None


def pick_asset(rel):
    """从 release 里挑一个 DMG。挑不到返回 None（宁可说「这次没法自动更新」）。"""
    for a in (rel or {}).get("assets") or []:
        name = str(a.get("name") or "")
        if name.lower().endswith(".dmg"):
            return {"name": name, "url": a.get("browser_download_url"),
                    "size": int(a.get("size") or 0)}
    return None


def short_notes(body, limit=900):
    """发版正文当更新说明用: 去掉图片/HTML 噪声, 超长就截断。"""
    t = str(body or "")
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit in ("B", "KB") else "%.1f %s" % (n, unit)
        n /= 1024.0


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def _open(url, timeout, opener=None):
    """统一出口: 允许测试注入 opener(url, timeout) -> bytes。"""
    if opener is not None:
        return opener(url, timeout)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check(local_version, opener=None):
    """看有没有新版本。

    返回 dict: ok / why / version / tag / notes / url / size / sha256 / newer。
    ok=False 时 why 是给用户看的一句话（网络不通、限流、这次没带 DMG…）。
    """
    out = {"ok": False, "why": "", "version": "", "tag": "", "notes": "",
           "url": "", "size": 0, "asset": "", "sha256": None, "newer": False}
    try:
        raw = _open(API_LATEST, API_TIMEOUT, opener=opener)
        rel = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            out["why"] = "还没发过版本"
        elif e.code in (403, 429):
            out["why"] = "GitHub 限流了，过一会儿再试"
        else:
            out["why"] = "GitHub 返回 %d" % e.code
        return out
    except Exception as e:                       # 无网络 / DNS / 证书 / 超时
        out["why"] = "连不上 GitHub（%s）" % e.__class__.__name__
        return out

    tag = str(rel.get("tag_name") or "")
    out["tag"] = tag
    out["version"] = tag.lstrip("vV") or str(rel.get("name") or "")
    out["notes"] = short_notes(rel.get("body"))
    out["ok"] = True
    out["newer"] = is_newer(tag, local_version)
    if not out["newer"]:
        return out

    a = pick_asset(rel)
    if not a or not a["url"]:
        # ★必须是 ok=False: 否则上层会拿着空 url 去下载 (自检里就是这么抓出来的)
        out["ok"] = False
        out["why"] = "这个版本没传安装包，只能去 GitHub 页面手动下载"
        return out
    out["url"], out["size"] = a["url"], a["size"]
    out["asset"] = a.get("name") or ""       # 日志和弹窗里报文件名, 比链接好认
    out["sha256"] = parse_sha256(rel.get("body"))
    return out


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(url, dest, progress=None, opener=None, expected_sha=None, timeout=DL_TIMEOUT):
    """下载到 dest。边下边算 SHA256；给了 expected_sha 就在下载过程中比对。

    返回 (path, sha256)。校验不过抛 UpdateError（并且把半成品删掉）。
    """
    h = hashlib.sha256()
    got = 0
    tmp = dest + ".part"
    try:
        if opener is not None:
            data = opener(url, timeout)
            with open(tmp, "wb") as f:
                f.write(data)
            h.update(data)
            got = len(data)
            if progress:
                progress(got, got)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                while True:
                    b = resp.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    h.update(b)
                    got += len(b)
                    if progress:
                        progress(got, total)
        digest = h.hexdigest()
        if expected_sha and digest != expected_sha:
            raise UpdateError("下载到的文件校验值对不上（发版正文说 %s，实际 %s）"
                              % (expected_sha[:12], digest[:12]))
        os.replace(tmp, dest)
        return dest, digest
    except UpdateError:
        _unlink(tmp)
        raise
    except Exception as e:
        _unlink(tmp)
        raise UpdateError("下载失败（%s）" % e.__class__.__name__)


def _unlink(path):
    try:
        os.unlink(path)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 换包
# --------------------------------------------------------------------------

def installer_script(app_path, dmg_path, pid, log_path=UPDATE_LOG):
    """生成「退出后换包」的 shell 脚本内容。

    规矩：**旧包绝不先删**。改名备份, 新包就位/启动失败就改名回来 ——
    最坏的结果是「更新没成功, 但程序还是原来那个」。
    """
    q = shlex.quote
    return """#!/bin/sh
# Mipad DP Touch 更新脚本 —— 由 App 写出, App 退出后执行。可以随时删掉。
set -u
APP={app}
DMG={dmg}
PID={pid}
LOG={log}
log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }}
log "更新开始: $DMG"
i=0
while kill -0 "$PID" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 120 ]; then log "等 App 退出超时, 放弃"; rm -f "$DMG"; exit 1; fi
  sleep 0.5
done
MNT=$(mktemp -d /tmp/mipad-update.XXXXXX) || {{ log "建不了挂载点"; exit 2; }}
if ! hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" >>"$LOG" 2>&1; then
  log "挂载 DMG 失败"; rm -rf "$MNT"; exit 3
fi
SRC=$(ls -d "$MNT"/*.app 2>/dev/null | head -1)
NEW="$APP.new"
BAK="$APP.bak"
rm -rf "$NEW" "$BAK"
if [ -z "$SRC" ] || ! ditto "$SRC" "$NEW" >>"$LOG" 2>&1; then
  log "从 DMG 里拷新版本失败"; hdiutil detach "$MNT" >/dev/null 2>&1; rm -rf "$MNT" "$NEW"; exit 4
fi
hdiutil detach "$MNT" >/dev/null 2>&1
rm -rf "$MNT"
if ! codesign --verify "$NEW" >>"$LOG" 2>&1; then
  log "新版本签名校验没过, 不动现有安装"; rm -rf "$NEW"; exit 5
fi
if ! mv "$APP" "$BAK" >>"$LOG" 2>&1; then
  log "旧版本改名失败(权限?), 放弃"; rm -rf "$NEW"; exit 6
fi
if ! mv "$NEW" "$APP" >>"$LOG" 2>&1; then
  log "新版本就位失败, 回滚"; mv "$BAK" "$APP" >>"$LOG" 2>&1; rm -rf "$NEW"; exit 7
fi
if /usr/bin/open -a "$APP" >>"$LOG" 2>&1; then
  log "已重新打开, 更新完成"
  sleep 5
  rm -rf "$BAK"
  rm -f "$DMG"
else
  log "打不开新版本, 回滚到旧版"
  rm -rf "$APP"
  mv "$BAK" "$APP" >>"$LOG" 2>&1
  /usr/bin/open -a "$APP" >>"$LOG" 2>&1
  exit 8
fi
exit 0
""".format(app=q(app_path), dmg=q(dmg_path), pid=int(pid), log=q(log_path))


def write_installer(app_path, dmg_path, pid, log_path=UPDATE_LOG):
    """把脚本写到临时文件(可执行), 返回路径。"""
    fd, path = tempfile.mkstemp(prefix="mipad-update-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(installer_script(app_path, dmg_path, pid, log_path=log_path))
    os.chmod(path, 0o755)
    return path


def new_dmg_path(version):
    """放下载的临时位置: 带版本号, 方便出问题时人工看。"""
    return os.path.join(tempfile.gettempdir(), "Mipad-DP-Touch-%s.dmg" % (version or "new"))


# --------------------------------------------------------------------------
# 离线自检(装机版跑不动 GUI 时也能验逻辑)
# --------------------------------------------------------------------------

def selftest_offline():
    """返回 (ok, lines)。纯函数 + 假 opener, 不碰网络。"""
    fails = []

    def ck(name, got, want):
        if got != want:
            fails.append("%s: 得到 %r, 期望 %r" % (name, got, want))

    ck("parse v1.1.0", parse_version("v1.1.0"), (1, 1, 0))
    ck("parse 1.1", parse_version("1.1"), (1, 1))
    ck("parse 带后缀", parse_version("1.2.0-beta.3"), (1, 2, 0))
    ck("parse 空", parse_version(""), ())
    ck("newer 1.1>1.0.0", is_newer("v1.1", "1.0.0"), True)
    ck("newer 1.0.0 不>1.0.0", is_newer("v1.0.0", "1.0.0"), False)
    ck("newer 不认识的格式不当新", is_newer("nightly", "1.0.0"), False)
    ck("newer 1.0.10>1.0.9", is_newer("v1.0.10", "1.0.9"), True)
    _body = "安装：\n\nSHA256  `%s`\n" % ("a1" * 32)
    ck("抓 SHA256", parse_sha256(_body), "a1" * 32)
    ck("没有 SHA256", parse_sha256("就是一段话"), None)
    ck("挑 DMG", (pick_asset({"assets": [{"name": "x.zip", "browser_download_url": "u1"},
                                         {"name": "Mipad-DP-Touch-1.1.0.dmg",
                                          "browser_download_url": "u2", "size": 7}]}) or {}).get("url"), "u2")
    ck("没有 DMG", pick_asset({"assets": [{"name": "x.zip"}]}), None)
    ck("正文截断", len(short_notes("x" * 3000)) <= 901, True)

    # 假 API: v9.9.9, 正文带校验值, 有一个 DMG 资产
    _rel = {"tag_name": "v9.9.9", "name": "9.9.9", "body": "改动：\n\nSHA256  %s\n" % ("b2" * 32),
            "assets": [{"name": "Mipad-DP-Touch-9.9.9.dmg",
                        "browser_download_url": "https://example.invalid/a.dmg", "size": 1234}]}
    r = check("1.0.0", opener=lambda u, t: json.dumps(_rel).encode())
    ck("check 有新版本", r["newer"], True)
    ck("check 版本号", r["version"], "9.9.9")
    ck("check 拿到下载地址", r["url"], "https://example.invalid/a.dmg")
    ck("check 拿到校验值", r["sha256"], "b2" * 32)
    r2 = check("9.9.9", opener=lambda u, t: json.dumps(_rel).encode())
    ck("check 同版本不算新", r2["newer"], False)

    def _boom(u, t):
        raise urllib.error.URLError("没网")

    r3 = check("1.0.0", opener=_boom)
    ck("check 断网不炸", r3["ok"], False)
    ck("check 断网有理由", bool(r3["why"]), True)

    # 下载 + 校验(注入 opener): 校验不过必须抛, 且不留半成品
    _payload = b"not really a dmg" * 100
    _good = hashlib.sha256(_payload).hexdigest()
    _dest = os.path.join(tempfile.gettempdir(), "mipad-selftest-dl.bin")
    _seen = []
    p, d = download("https://example.invalid/a.dmg", _dest,
                    progress=lambda a, b: _seen.append((a, b)),
                    opener=lambda u, t: _payload, expected_sha=_good)
    ck("下载返回校验值", d, _good)
    ck("下载落盘", os.path.exists(p), True)
    ck("进度回调被调到", bool(_seen), True)
    try:
        download("https://example.invalid/a.dmg", _dest, opener=lambda u, t: _payload,
                 expected_sha="00" * 32)
        fails.append("校验值不对时没有报错")
    except UpdateError:
        pass
    ck("校验不过不留半成品", os.path.exists(_dest + ".part"), False)
    _unlink(_dest)

    # 更新脚本: 关键性质 —— 旧包先改名(不是删), 每步失败都有回滚
    s = installer_script("/Applications/Mipad DP Touch.app", "/tmp/x.dmg", 4242)
    for want in ("kill -0", "hdiutil attach", "codesign --verify",
                 'mv "$APP" "$BAK"', "mv \"$BAK\" \"$APP\"", "/usr/bin/open -a",
                 "/Applications/Mipad DP Touch.app"):
        if want not in s:
            fails.append("更新脚本缺少关键步骤: %s" % want)
    if s.count('rm -rf "$APP"') > 1:
        # 只有「新版本打不开」这条回滚路径允许删新包; 其余地方一律只改名不删
        fails.append("更新脚本里有 %d 处直接删 $APP" % s.count('rm -rf "$APP"'))
    if s.index('mv "$APP" "$BAK"') < s.index('codesign --verify'):
        fails.append("更新脚本顺序不对: 应该先验签名再动现有安装")
    if "mipad-update-" not in write_installer("/Applications/X.app", "/tmp/y.dmg", 1):
        fails.append("write_installer 返回的路径不对")
    if '"' not in installer_script("/Applications/a b.app", "/tmp/x y.dmg", 1):
        fails.append("带空格的路径没有被引号包住")

    lines = ["离线自检: %s" % ("全部通过" if not fails else "%d 项不通过" % len(fails))]
    lines += ["  !! %s" % f for f in fails]
    return (not fails), lines
