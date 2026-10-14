#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新这条链路的离线自检 —— 不联网、不换包, 只钉住能被钉住的部分。

为什么值得单独一套: 自更新里「换包」那一步是唯一真的会动到磁盘上那个 .app 的操作,
一旦逻辑错了没有第二次机会; 而它前面每一步 (版本号比较、正文里抓 SHA256、下载校验、
脚本生成) 都能在进程内用假 opener 完整跑一遍。真发版时才需要联网的那两档在
src/dptouch.py 的 DPTOUCH_SELFTEST_UPDATE=live|dry 里。

用法: python3 tools/selftest_update.py     (全过 exit 0, 有失败 exit 1)
"""

import json
import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import dptouch_update as U            # noqa: E402

PASSED = [0]
FAILED = []


def check(name, got, want):
    if got == want:
        PASSED[0] += 1
    else:
        FAILED.append((name, want, got))


def ok(name, cond, note=""):
    check(name + ((" (%s)" % note) if note else ""), bool(cond), True)


HEX = "a" * 64
BODY = ("## 安装\n\n1. 下载 `Mipad-DP-Touch-1.1.0.dmg`（14 MB）\n\n"
        "校验（macOS）：\n\n```sh\nshasum -a 256 ~/Downloads/Mipad-DP-Touch-1.1.0.dmg\n"
        "# SHA256  " + HEX + "\n```\n")


def rel(tag="v1.1.0", body=BODY, assets=None):
    if assets is None:
        assets = [{"name": "Mipad-DP-Touch-1.1.0.dmg", "size": 14067442, "browser_download_url":
                   "https://github.com/InYvn/mipad-dp-touch/releases/download/v1.1.0/x.dmg"}]
    return json.dumps({"tag_name": tag, "name": "Mipad DP Touch " + tag.lstrip("v"),
                       "body": body, "assets": assets}).encode("utf-8")


def opener_for(payload, exc=None):
    def _op(url, timeout):
        if exc is not None:
            raise exc
        return payload
    return _op


def test_version():
    check("parse_version('1.0.0')", U.parse_version("1.0.0"), (1, 0, 0))
    check("parse_version('v1.1')", U.parse_version("v1.1"), (1, 1))
    check("parse_version('v1.1.0')", U.parse_version("v1.1.0"), (1, 1, 0))
    check("parse_version 去掉预发布后缀", U.parse_version("1.0.0-beta.1"), (1, 0, 0))
    check("parse_version('')", U.parse_version(""), ())
    check("parse_version('版本')", U.parse_version("版本"), ())
    ok("1.1.0 > 1.0.0", U.is_newer("v1.1.0", "1.0.0"))
    ok("1.0.10 比 1.0.9 新 (数字比较, 不是字符串)", U.is_newer("1.0.10", "1.0.9"))
    ok("同版本不算新", not U.is_newer("v1.0.0", "1.0.0"))
    ok("旧版本不算新", not U.is_newer("v0.9.9", "1.0.0"))
    ok("1.1 比 1.0.0 新", U.is_newer("1.1", "1.0.0"))
    ok("解析不出来时不当成有新版本", not U.is_newer("junk", "1.0.0"))


def test_sha_and_notes():
    check("正文里抓 SHA256", U.parse_sha256(BODY), HEX)
    # 大写也认, 但统一返回小写 —— 要能和 hashlib.hexdigest() 直接比
    check("大写 SHA256 也认, 并归一成小写", U.parse_sha256("SHA256  " + HEX.upper()), HEX)
    check("长度不对 -> None", U.parse_sha256("SHA256  deadbeef"), None)
    check("没有校验段 -> None", U.parse_sha256("这次忘了写校验"), None)
    # 一个 release 里同时挂着平板侧模块 zip: 正文里有两个校验值, 要挑 DMG 那行
    check("两个校验值 -> 挑 .dmg 那行",
          U.parse_sha256("SHA256  %s  Mipad-DP-Touch-1.2.dmg\nSHA256  %s  mipad-dp-touch-module-v1.0.zip"
                         % (HEX, "0" * 64)), HEX)
    check("表格写法（表头是 SHA256、值在数据行）-> 取 .dmg 行",
          U.parse_sha256("| 文件 | 说明 | SHA256 |\n| --- | --- | --- |\n| `X.dmg` | App | `%s` |\n| `mod.zip` | 模块 | `%s` |"
                         % (HEX, "0" * 64)), HEX)
    check("表格里模块在前也不误取",
          U.parse_sha256("| 文件 | SHA256 |\n| `mod.zip` | `%s` |\n| `X.dmg` | `%s` |"
                         % ("0" * 64, HEX)), HEX)
    check("模块行在前也不误取",
          U.parse_sha256("SHA256  %s  mipad-dp-touch-module-v1.0.zip\nSHA256  %s  Mipad-DP-Touch-1.2.dmg"
                         % ("0" * 64, HEX)), HEX)
    ok("更新说明是截断过的", len(U.short_notes("x" * 5000)) <= 1000)


def test_assets():
    good = {"assets": [{"name": "a.zip", "size": 1, "browser_download_url": "u1"},
                       {"name": "b.dmg", "size": 2, "browser_download_url": "u2"}]}
    a = U.pick_asset(good)
    check("优先挑 .dmg", (a or {}).get("url"), "u2")
    check("没有 dmg -> None", U.pick_asset({"assets": [{"name": "a.zip", "url": "u"}]}), None)
    check("assets 为空 -> None", U.pick_asset({"assets": []}), None)


def test_check():
    r = U.check("1.0.0", opener=opener_for(rel()))
    ok("查到新版本", r["ok"] and r["newer"])
    check("版本号取自 tag", r["version"], "1.1.0")
    check("下载链接用 API 给的, 不自己拼", r["url"],
         "https://github.com/InYvn/mipad-dp-touch/releases/download/v1.1.0/x.dmg")
    check("大小来自资产", r["size"], 14067442)
    check("SHA256 来自发版正文", r["sha256"], HEX)
    check("资产名也带回来 (日志里报文件名)", r["asset"], "Mipad-DP-Touch-1.1.0.dmg")
    ok("说明文字带过来了", "安装" in (r["notes"] or ""))

    r = U.check("1.1.0", opener=opener_for(rel()))
    ok("同版本 -> ok 但没有新版本", r["ok"] and not r["newer"])

    r = U.check("1.0.0", opener=opener_for(None, urllib.error.HTTPError(
        "u", 403, "rate limited", {}, None)))
    ok("限流 -> 失败且说得像人话", (not r["ok"]) and ("限流" in r["why"]), r["why"])

    r = U.check("1.0.0", opener=opener_for(None, urllib.error.URLError("no net")))
    ok("连不上 -> 失败且不抛异常", (not r["ok"]) and r["why"], r["why"])

    r = U.check("1.0.0", opener=opener_for(rel(assets=[{"name": "s.zip", "url": "u"}])))
    ok("新版本没带 dmg -> 失败并说明只能手动下", (not r["ok"]) and ("手动" in r["why"]), r["why"])

    r = U.check("1.0.0", opener=opener_for(b"{not json"))
    ok("返回不是 JSON -> 失败不崩", not r["ok"])

    r = U.check("1.0.0", opener=opener_for(rel(body="这次没写校验段")))
    ok("正文没写 SHA256 时照样能更新, 只是校验值缺失", r["ok"] and r["newer"]
       and r["sha256"] is None)


def test_download():
    tmp = tempfile.mkdtemp(prefix="dptouch-upd-test-")
    dest = os.path.join(tmp, "x.dmg")
    data = b"Mipad" * 1000
    import hashlib
    real = hashlib.sha256(data).hexdigest()
    prog = []
    p, sha = U.download("u", dest, progress=lambda g, t: prog.append((g, t)),
                        opener=opener_for(data), expected_sha=real)
    check("下载返回落盘路径", p, dest)
    check("下载算出的 SHA256", sha, real)
    check("落盘内容和下载到的一致", os.path.getsize(dest), len(data))
    ok("进度回调被叫过", prog and prog[-1] == (len(data), len(data)))
    ok("校验不过要抛错", True)
    try:
        U.download("u", os.path.join(tmp, "y.dmg"), opener=opener_for(data),
                   expected_sha="b" * 64)
        FAILED.append(("校验值不对必须抛 UpdateError", "UpdateError", "没抛"))
    except U.UpdateError:
        PASSED[0] += 1
    ok("校验失败不留半成品",
       not os.path.exists(os.path.join(tmp, "y.dmg.part"))
       and not os.path.exists(os.path.join(tmp, "y.dmg")))
    try:
        U.download("u", os.path.join(tmp, "z.dmg"), opener=opener_for(None,
                   urllib.error.URLError("no net")))
        FAILED.append(("下载失败必须抛 UpdateError", "UpdateError", "没抛"))
    except U.UpdateError:
        PASSED[0] += 1


def test_installer():
    s = U.installer_script("/Applications/Mipad DP Touch.app",
                           "/tmp/Mipad-DP-Touch-1.1.0.dmg", 4242)
    ok("先等旧进程退出", "kill -0" in s and "4242" in s)
    ok("挂载用的挂载点是临时目录", "mktemp -d" in s and "hdiutil attach" in s)
    ok("挂载完一定摘掉 (两条路径各一次)", s.count("hdiutil detach") >= 2)
    ok("验签名在动现有安装之前",
       s.index("codesign --verify") < s.index('mv "$APP" "$BAK"'))
    ok("旧包只改名不删", 'mv "$APP" "$BAK"' in s and 'rm -rf "$APP"' in s)
    ok("启动失败要有回滚", 'mv "$BAK" "$APP"' in s)
    ok("路径带引号", s.count('"$APP"') >= 2 and s.count('"$DMG"') >= 1)
    ok("日志写到自己的日志文件", U.UPDATE_LOG in s)
    _sp = U.installer_script("/Applications/a b.app", "/tmp/c d.dmg", 1)
    ok("带空格的路径被单引号包住", "'/Applications/a b.app'" in _sp and "'/tmp/c d.dmg'" in _sp)
    p = U.write_installer("/Applications/x.app", "/tmp/y.dmg", 1)
    ok("脚本落地可执行", os.path.exists(p) and os.access(p, os.X_OK), p)
    ok("脚本名可辨认", "mipad-update-" in os.path.basename(p))
    try:
        os.unlink(p)
    except Exception:
        pass


def test_paths():
    p = U.new_dmg_path("1.1.0")
    ok("安装包放临时目录", p.startswith(tempfile.gettempdir()), p)
    ok("文件名带版本号", "1.1.0" in p and p.endswith(".dmg"))
    p2 = U.new_dmg_path("")
    ok("版本号缺了也能生成合法名字", p2.endswith(".dmg") and "None" not in p2, p2)


def main():
    print("=== 检查更新: 离线自检 ===")
    for fn in (test_version, test_sha_and_notes, test_assets, test_check,
               test_download, test_installer, test_paths):
        fn()
    _o, lines = U.selftest_offline()
    for l in lines:
        print("  " + l)
    if not _o:
        FAILED.append(("模块自带的离线自检", "全过", "有失败"))
    print("-" * 64)
    if FAILED:
        print("失败 %d 项 / 通过 %d 项" % (len(FAILED), PASSED[0]))
        for name, want, got in FAILED:
            print("  ✗ %s\n     期望 %s\n     实际 %s" % (name, want, got))
        return 1
    print("全部通过 (%d 项断言)" % PASSED[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
