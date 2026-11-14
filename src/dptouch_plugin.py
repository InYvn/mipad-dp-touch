#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可选插件：注册表 / 下载 / 校验 / 安装 / 加载。

设计约束（用户定过的）
----------------------
* **不随 App 打包**：插件只在用户点「安装」后，从公开仓库现场拉。
* 安装前必须让用户看到来源、版本、大小；不偷偷装。
* 完整性：发版正文里必须写该资产的 SHA256，缺了就拒绝安装。
* 进程内加载：插件跑在 dptouch 进程里，共用它的「设备控制和数据访问」授权，
  不额外产生 Dock / 菜单栏图标。

注册表
------
`PLUGINS` 是唯一的事实来源：id / 仓库 / 资产名规则 / 入口包名 / 最低宿主版本。
新增插件只需在这里加一条，UI 与安装流程都按 id 走。

目录布局::

    ~/Library/Application Support/MipadDPTouch/plugins/
        touchkeys/installed.json + 0.1.0/…
        touchbar/installed.json + 0.1.0/…

这一层不 import AppKit，纯函数 + 文件操作，便于离线自检。
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

UA = "MipadDPTouch"
API_TIMEOUT = 15
MAX_ZIP_BYTES = 20 << 20          # 插件包不该超过 20MB

PLUGINS = (
    {
        "id": "touchkeys",
        "name": "屏幕键盘",
        "brand": "TouchKeys",
        "blurb": "触屏副屏用的浮动键盘：点按合成真实按键，支持中英切换与焦点跟随。",
        "repo": "InYvn/TouchKeys",
        "asset_re": r"^touchkeys-plugin-(.+)\.zip$",
        "entry": "touchkeys_plugin",
        "min_host": "2.0.0",
        "symbol": "keyboard",
    },
    {
        "id": "touchbar",
        "name": "浮动工具栏",
        "brand": "TouchBar",
        "blurb": "跟随前台 App 的浮动工具条：按钮来自它自己的工具栏，点按即执行；可拖动、吸附侧边、调透明度。",
        "repo": "InYvn/TouchBar",
        "asset_re": r"^touchbar-plugin-(.+)\.zip$",
        "entry": "touchbar_plugin",
        "min_host": "2.1.0",
        "symbol": "rectangle.topthird.inset.filled",
    },
)

DEFAULT_ID = "touchkeys"

# 外部工具：不是插件，装不进 App 进程。只做「下载官方安装包 + 打开官网」，
# 不镜像、不打包、不静默安装 —— 版权上只做跳转/代下，不做分发。
TOOLS = (
    {
        "id": "hyperconnect",
        "name": "小米互联服务",
        "tag": "小米官方",
        "blurb": "小米互联服务是一款运行在苹果设备上，并能和小米设备之间进行文件互传、数据流转和桌面共享等跨端协同和控制的应用。",
        "url": "https://hyperconnect.hyperos.mi.com/hyper-connect/HyperConnect_AI.dmg",
        "page": "https://os.mi.com/continuity/abilities/ab0016",
        "symbol": "display",
        "min_host": "2.0.0",
    },
)


def tools():
    """全部外部工具（只读）。"""
    return TOOLS


def tool(tid):
    for t in TOOLS:
        if t["id"] == tid:
            return t
    raise PluginError("未知工具: %s" % tid)


def download_file(url, dest, progress=None, timeout=1800):
    """流式下载（不校验哈希）：外部工具只从官方地址拉，下完由调用方验签名。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress is not None:
                    try:
                        progress(done, total)
                    except Exception:
                        pass
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# 旧代码兼容别名
PLUGIN_ID = DEFAULT_ID
PLUGIN_NAME = "屏幕键盘（TouchKeys）"
REPO = PLUGINS[0]["repo"]
RELEASES_PAGE = "https://github.com/%s/releases" % REPO


class PluginError(Exception):
    """带可读理由的失败。"""


# ---------------------------------------------------------------- 注册表

def registry():
    return [dict(p) for p in PLUGINS]


def get(pid):
    for p in PLUGINS:
        if p["id"] == pid:
            return dict(p)
    raise PluginError("未知插件 id：%r" % (pid,))


def plugin_ids():
    return [p["id"] for p in PLUGINS]


def releases_page(pid=DEFAULT_ID):
    return "https://github.com/%s/releases" % get(pid)["repo"]


# ---------------------------------------------------------------- 纯函数

def parse_version(s):
    """'v0.1.0' -> (0,1,0)；后缀丢掉。"""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(s or ""))
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


def parse_asset_sha256(body, asset_name):
    """从发版正文里抓该资产的 SHA256；找不到返回 None（调用方必须拒绝安装）。"""
    if not body:
        return None
    hexre = re.compile(r"\b([0-9a-fA-F]{64})\b")
    for line in str(body).splitlines():
        if asset_name and asset_name.lower() not in line.lower():
            continue
        m = hexre.search(line)
        if m:
            return m.group(1).lower()
    return None


def pick_asset(release, pid=DEFAULT_ID):
    """从 release JSON 里挑该插件的 zip；返回 (name, url, size) 或 None。"""
    pat = re.compile(get(pid)["asset_re"], re.I)
    for a in release.get("assets") or []:
        name = a.get("name") or ""
        if pat.match(name):
            return (name, a.get("browser_download_url") or "", int(a.get("size") or 0))
    return None


def _open(url, timeout=API_TIMEOUT, opener=None):
    if opener is not None:
        return opener(url, timeout)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check(pid=DEFAULT_ID, opener=None):
    """读该插件仓库的 latest release。返回 ok/why/version/url/size/sha256/notes/asset/page。"""
    meta = get(pid)
    api = "https://api.github.com/repos/%s/releases/latest" % meta["repo"]
    page = releases_page(pid)
    out = {"ok": False, "why": "", "version": "", "url": "", "size": 0,
           "sha256": None, "notes": "", "asset": "", "page": page, "id": pid}
    try:
        raw = _open(api, opener=opener)
        rel = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception as e:
        out["why"] = "取插件发版信息失败：%r" % (e,)
        return out
    picked = pick_asset(rel, pid)
    if picked is None:
        out["why"] = "这个发版里没有匹配 %s 的资产" % meta["asset_re"]
        return out
    name, url, size = picked
    out.update({"ok": True, "version": rel.get("tag_name") or rel.get("name") or "",
                "url": url, "size": size, "asset": name,
                "notes": (rel.get("body") or "")[:800],
                "sha256": parse_asset_sha256(rel.get("body") or "", name)})
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


def download(url, dest, sha256=None, opener=None, timeout=600):
    """下载并校验；sha256 为 None 时**直接失败**（不允许无校验安装）。"""
    if not sha256:
        raise PluginError("发版正文没有该资产的 SHA256，拒绝安装。")
    h = hashlib.sha256()
    n = 0
    with open(dest, "wb") as f:
        if opener is not None:
            data = opener(url, timeout)
            chunks = [data]
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            chunks = urllib.request.urlopen(req, timeout=timeout)
        try:
            for b in chunks:
                n += len(b)
                if n > MAX_ZIP_BYTES:
                    raise PluginError("插件包超过 %d MB，中止。" % (MAX_ZIP_BYTES >> 20))
                h.update(b)
                f.write(b)
        finally:
            if opener is None:
                chunks.close()
    got = h.hexdigest()
    if got.lower() != sha256.lower():
        os.unlink(dest)
        raise PluginError("SHA256 不符：期望 %s…，实际 %s…" % (sha256[:12], got[:12]))
    return dest, got


def read_manifest(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_manifest(meta, host_version, pid=DEFAULT_ID):
    """检查 id / api / min_host；不通过抛 PluginError。"""
    if meta.get("id") != pid:
        raise PluginError("插件 id 不符：%r（期望 %r）" % (meta.get("id"), pid))
    if int(meta.get("api", 0)) != 1:
        raise PluginError("插件 API 版本 %r 不受支持" % (meta.get("api"),))
    mh = meta.get("min_host") or get(pid).get("min_host") or "0.0.0"
    if parse_version(host_version) < parse_version(mh):
        raise PluginError("需要 App ≥ %s，当前 %s" % (mh, host_version))
    if not meta.get("version"):
        raise PluginError("插件清单缺少 version")
    return meta


def safe_extract(zip_path, dest):
    """解压并挡住 ../ 与绝对路径。"""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise PluginError("插件包里有非法路径：%r" % info.filename)
        z.extractall(dest)


# ---------------------------------------------------------------- 安装状态

def plugins_dir():
    return os.path.expanduser("~/Library/Application Support/MipadDPTouch/plugins")


def plugin_root(pid=DEFAULT_ID, base=None):
    return os.path.join(base or plugins_dir(), pid)


def installed(pid=DEFAULT_ID, base=None):
    """读 installed.json；没装或坏了返回 None。"""
    root = plugin_root(pid, base)
    p = os.path.join(root, "installed.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") and os.path.isdir(os.path.join(root, d["version"])):
            return d
    except Exception:
        pass
    return None


def install_from_zip(zip_path, source, host_version, base=None, sha256=None, pid=DEFAULT_ID):
    """校验 → 解压到 <root>/<version> → 写 installed.json（原子替换）。"""
    root = plugin_root(pid, base)
    os.makedirs(root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".install-", dir=root)
    try:
        safe_extract(zip_path, tmp)
        meta = read_manifest(os.path.join(tmp, "plugin.json"))
        validate_manifest(meta, host_version, pid)
        ver = str(meta["version"])
        final = os.path.join(root, ver)
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.replace(tmp, final)
        rec = {"id": pid, "version": ver, "dir": ver, "source": source,
               "sha256": sha256 or sha256_file(zip_path),
               "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(os.path.join(root, "installed.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        # 只保留当前版本
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if name not in (ver, "installed.json") and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def uninstall(pid=DEFAULT_ID, base=None):
    root = plugin_root(pid, base)
    shutil.rmtree(root, ignore_errors=True)
    return not os.path.exists(root)


def load(pid=DEFAULT_ID, base=None):
    """import 已安装的插件包，返回 module。"""
    rec = installed(pid, base)
    if rec is None:
        raise PluginError("插件尚未安装")
    entry = get(pid)["entry"]
    pkg_dir = os.path.join(plugin_root(pid, base), rec["version"])
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    for mod in list(sys.modules):
        if mod == entry or mod.startswith(entry + "."):
            del sys.modules[mod]
    spec = importlib.util.spec_from_file_location(
        entry, os.path.join(pkg_dir, entry, "__init__.py"),
        submodule_search_locations=[os.path.join(pkg_dir, entry)])
    if spec is None or spec.loader is None:
        raise PluginError("插件包结构不对，找不到 %s/__init__.py" % entry)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[entry] = mod
    spec.loader.exec_module(mod)
    return mod


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

    ck("注册表含两个插件", plugin_ids() == ["touchkeys", "touchbar"], plugin_ids())
    ck("按 id 取元数据", get("touchbar")["repo"] == "InYvn/TouchBar")
    try:
        get("nope")
        ck("未知 id 报错", False)
    except PluginError:
        ck("未知 id 报错", True)

    body = ("## 插件\n"
            "touchkeys-plugin-0.1.0.zip  sha256: %s\n" % ("a1" * 32))
    ck("抓 SHA256", parse_asset_sha256(body, "touchkeys-plugin-0.1.0.zip") == "a1" * 32)
    ck("没有 SHA256", parse_asset_sha256("随便写写", "x.zip") is None)
    ck("版本比较", is_newer("v0.2.0", "0.1.0") and not is_newer("v0.1.0", "0.1.0"))

    rel = {"tag_name": "v0.1.0", "body": body,
           "assets": [{"name": "touchkeys-plugin-0.1.0.zip",
                       "browser_download_url": "https://example.invalid/t.zip",
                       "size": 1234}]}
    r = check("touchkeys", opener=lambda u, t: json.dumps(rel).encode())
    ck("check 成功", r["ok"], r.get("why"))
    ck("check 取到地址", r["url"].endswith("t.zip"))
    ck("check 取到 sha", r["sha256"] == "a1" * 32)

    rel2 = {"tag_name": "v0.1.0", "body": "touchbar-plugin-0.1.0.zip sha256: " + "b2" * 32,
            "assets": [{"name": "touchbar-plugin-0.1.0.zip",
                        "browser_download_url": "https://example.invalid/tb.zip", "size": 99}]}
    r2 = check("touchbar", opener=lambda u, t: json.dumps(rel2).encode())
    ck("第二个插件按自己的资产名匹配",
       r2["ok"] and r2["asset"] == "touchbar-plugin-0.1.0.zip", r2.get("why"))

    try:
        download("https://example.invalid/t.zip", os.path.join(tempfile.gettempdir(), "x.zip"),
                 sha256=None, opener=lambda u, t: b"data")
        ck("无 sha 拒绝", False)
    except PluginError:
        ck("无 sha 拒绝", True)

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    print(json.dumps(selftest_offline(), ensure_ascii=False))
