#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI → 现有设置接口的受控桥。

不直接写 config.json，也不碰 `_apply`：只调用 dptouch.py 里已有的
set_bind / set_gain / set_natural / set_hold_ms / set_rc_hold /
set_input_mode / set_edid_correct。

四件事：
1. 白名单清单（喂给模型 / 生成差异）；
2. 预览（旧值 → 新值 + 副作用提示）；
3. 事务执行：快照 → 调用 → **读回验证** → 失败逆序回滚；
4. 历史与撤销（JSONL 追加，撤销写一条反向记录）。

为什么必须读回验证：现有 set_* 对非法值是**静默 return**，不抛异常
（见 dptouch.py set_gain / set_hold_ms 等）。只调不验会出现"AI 说改好了、
实际没变"——这正是需求禁止的。
"""

import json
import os
import time
import uuid

import ai_protocol as P


MAX_OPS = 8          # 一次最多改这么多项，防止模型塞一堆操作


class Change(object):
    __slots__ = ("key", "title", "old", "new", "warn", "implicit")

    def __init__(self, key, title, old, new, warn="", implicit=False):
        self.key = key
        self.title = title
        self.old = old
        self.new = new
        self.warn = warn
        self.implicit = implicit

    def as_dict(self):
        return {"key": self.key, "title": self.title, "old": self.old,
                "new": self.new, "warn": self.warn, "implicit": self.implicit}

    def __repr__(self):
        return "<Change %s %r→%r%s>" % (self.key, self.old, self.new,
                                        " (implicit)" if self.implicit else "")


class Result(object):
    def __init__(self):
        self.ok = False
        self.applied = []          # [Change]
        self.errors = []           # [str]
        self.rolled_back = []      # [str]
        self.rollback_failed = []  # [str]
        self.txn = ""

    def as_dict(self):
        return {"ok": self.ok, "txn": self.txn,
                "applied": [c.as_dict() for c in self.applied],
                "errors": list(self.errors),
                "rolled_back": list(self.rolled_back),
                "rollback_failed": list(self.rollback_failed)}


# 每项：配置键、标题、取值、调用、风险提示
def _specs():
    return {
        "set_bind": {
            "title": lambda op: "笔 · %s" % _gesture_title(op.get("gesture")),
            "key": lambda op: "bind_" + str(op.get("gesture")),
            "warn": "只影响手写笔，不影响系统原生手指触控",
        },
        "set_gain": {"title": lambda op: "滚动速度", "key": lambda op: "gain",
                     "warn": ""},
        "set_natural": {"title": lambda op: "滚动方向", "key": lambda op: "natural",
                        "warn": ""},
        "set_hold_ms": {"title": lambda op: "停住再滑判定", "key": lambda op: "hold_ms",
                        "warn": ""},
        "set_rc_hold": {"title": lambda op: "停住不动判定", "key": lambda op: "rc_hold_ms",
                        "warn": ""},
        "set_input_mode": {"title": lambda op: "输入模式", "key": lambda op: "input_mode",
                           "warn": "切换后手写笔会断开重连一下"},
        "set_edid_correct": {"title": lambda op: "位置校正", "key": lambda op: "edid_correct",
                             "warn": "会重推 EDID，屏幕可能闪一下"},
    }


_GESTURE_TITLES = {"tap": "轻点", "swipe": "快速滑动",
                   "hold_swipe": "停住再滑", "hold": "停住不动"}
_ACTION_TITLES = {"none": "无", "left": "左键", "right": "右键", "middle": "中键",
                  "double": "双击", "space": "空格", "back": "后退",
                  "screen": "截图", "scroll": "滚动", "drag": "拖拽"}


def _gesture_title(g):
    return _GESTURE_TITLES.get(g, str(g))


def action_title(a):
    return _ACTION_TITLES.get(a, str(a))


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except Exception:
            return False
    return a == b


class AISettingsBridge(object):
    def __init__(self, app, log=None, history_path=None):
        self.app = app
        self._log = log or (lambda _m: None)
        self.history_path = history_path or os.path.expanduser(
            "~/Library/Application Support/MipadDPTouch/ai_history.jsonl")

    # ------------------------------------------------------------ 清单
    def manifest(self):
        """允许 AI 改的全部设置（含当前值）。只读，不含任何危险项。"""
        cfg = self.app.cfg
        out = []
        for g in P.GESTURE_KEYS:
            out.append({
                "op": "set_bind", "key": "bind_" + g, "gesture": g,
                "title": "笔 · %s" % _gesture_title(g),
                "values": list(P.GESTURES[g]),
                "value_titles": {a: action_title(a) for a in P.GESTURES[g]},
                "current": cfg.get("bind_" + g),
                "note": "只影响手写笔，不影响系统原生手指触控",
            })
        out.append({"op": "set_gain", "key": "gain", "title": "滚动速度",
                    "values": list(P.GAINS), "current": cfg.get("gain"),
                    "note": "档位：慢 / 偏慢 / 标准 / 快 / 很快"})
        out.append({"op": "set_natural", "key": "natural", "title": "滚动方向",
                    "values": list(P.NATURAL), "current": cfg.get("natural"),
                    "value_titles": {"system": "跟随系统", "on": "始终自然",
                                     "off": "始终传统"}})
        out.append({"op": "set_hold_ms", "key": "hold_ms", "title": "停住再滑判定",
                    "values": list(P.HOLD_MS), "current": cfg.get("hold_ms")})
        out.append({"op": "set_rc_hold", "key": "rc_hold_ms", "title": "停住不动判定",
                    "values": list(P.RC_HOLD_MS), "current": cfg.get("rc_hold_ms"),
                    "note": "0 = 关"})
        out.append({"op": "set_input_mode", "key": "input_mode", "title": "输入模式",
                    "values": list(P.INPUT_MODES), "current": cfg.get("input_mode"),
                    "note": "切换会让笔断开重连一下"})
        out.append({"op": "set_edid_correct", "key": "edid_correct", "title": "位置校正",
                    "values": [True, False], "current": bool(cfg.get("edid_correct")),
                    "note": "会重推 EDID，屏幕可能闪一下"})
        return out

    def context_text(self):
        """给模型看的能力说明（固定上下文，体积小）。"""
        lines = [
            "可改设置（只能通过 operations 修改，值必须精确命中）：",
        ]
        for m in self.manifest():
            cur = m["current"]
            if isinstance(cur, float):
                cur = "%g" % cur
            lines.append("- %s：%s（当前：%s）" % (m["title"], m["values"], cur))
        lines += [
            "",
            "做不到的事（必须回 unsupported，不许假装能改）：",
            "- 手指双击 / 手指手势：原生触控模式下由 macOS 直接处理，笔的 bind_* 不控制手指。",
            "- 切换原生 / 兼容模拟触控模式：需要用户自己确认，AI 不改。",
            "- 改系统设置、装软件、执行命令、读写任意文件：都不做。",
            "",
            "回答要求：只输出一个 JSON 对象，不要多余文字。",
            '改设置：{"type":"settings_change","summary":"...","operations":[...]}',
            '只解释：{"type":"answer","text":"..."}',
            '做不到：{"type":"unsupported","reason":"...","why":"..."}',
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ 校验
    def _validated(self, ops):
        """入口强校验：不信调用方（插件 / 模型）传进来的结构。

        只做白名单 + 类型 + 枚举校验，返回规范化后的新列表；
        任何不合法的输入直接抛 ProtocolError，绝不进入执行阶段。
        """
        if not isinstance(ops, (list, tuple)):
            raise P.ProtocolError("operations 必须是数组")
        if not ops:
            raise P.ProtocolError("operations 不能为空")
        if len(ops) > MAX_OPS:
            raise P.ProtocolError("一次最多 %d 项操作" % MAX_OPS)
        return [P.validate_op(op) for op in ops]

    # ------------------------------------------------------------ 预览
    def preview(self, ops):
        """返回 [Change]；不修改任何状态。"""
        ops = self._validated(ops)
        cfg = self.app.cfg
        specs = _specs()
        out = []
        for op in ops:
            spec = specs[op["op"]]
            key = spec["key"](op)
            old = cfg.get(key)
            new = self._target(op)
            warn = spec["warn"]
            if _same(old, new):
                warn = "已经是这个值" + (("；" + warn) if warn else "")
            out.append(Change(key, spec["title"](op), old, new, warn))
            # set_bind(hold, ...) 的隐含副作用：rc_hold_ms=0 时自动打开 800ms
            if (op["op"] == "set_bind" and op["gesture"] == "hold"
                    and op["action"] != "none" and not int(cfg.get("rc_hold_ms") or 0)):
                out.append(Change("rc_hold_ms", "停住不动判定",
                                  cfg.get("rc_hold_ms"), 800,
                                  "绑定「停住不动」会自动打开这个判定", implicit=True))
        return out

    def _target(self, op):
        if op["op"] == "set_bind":
            return op["action"]
        return op["value"]

    # ------------------------------------------------------------ 执行
    def apply(self, ops, journal=True):
        ops = self._validated(ops)
        res = Result()
        res.txn = uuid.uuid4().hex[:12]
        changes = self.preview(ops)
        before = {}
        for c in changes:
            if c.key not in before:
                before[c.key] = c.old
        # 执行
        for op in ops:
            try:
                self._call(op)
            except Exception as e:
                res.errors.append("%s 执行失败：%r" % (op["op"], e))
        # 验证（静默 return 会在这里暴露）
        for c in changes:
            got = self.app.cfg.get(c.key)
            if not _same(got, c.new):
                res.errors.append("%s 没有生效（期望 %r，实际 %r）"
                                  % (c.title, c.new, got))
        if res.errors:
            self._rollback(before, res)
            return res
        res.ok = True
        res.applied = changes
        if journal:
            self._journal({"txn": res.txn, "ts": int(time.time()), "kind": "apply",
                           "ops": ops, "before": before,
                           "after": {c.key: c.new for c in changes},
                           "result": "ok"})
        return res

    def _call(self, op):
        name = op["op"]
        app = self.app
        if name == "set_bind":
            app.set_bind(op["gesture"], op["action"])
        elif name == "set_gain":
            app.set_gain(op["value"])
        elif name == "set_natural":
            app.set_natural(op["value"])
        elif name == "set_hold_ms":
            app.set_hold_ms(op["value"])
        elif name == "set_rc_hold":
            app.set_rc_hold(op["value"])
        elif name == "set_input_mode":
            app.set_input_mode(op["value"])
        elif name == "set_edid_correct":
            app.set_edid_correct(op["value"])
        else:
            raise ValueError("未知操作 %r" % (name,))

    def _rollback(self, before, res):
        """按旧值逆序回滚；能回多少回多少，回不掉的逐条报告。"""
        titles = {m["key"]: m["title"] for m in self.manifest()}

        def _t(k):
            return titles.get(k, k)
        specs = _specs()
        inv = []
        # 用 before 生成反向调用（不依赖 applied —— 失败时它可能是空的）
        for key, old in reversed(list(before.items())):
            inv.append((key, old))
        for key, old in inv:
            try:
                self._call(self._inverse(key, old))
                got = self.app.cfg.get(key)
                if _same(got, old):
                    res.rolled_back.append(_t(key))
                else:
                    res.rollback_failed.append("%s（期望回滚到 %r，实际 %r）"
                                               % (_t(key), old, got))
            except Exception as e:
                res.rollback_failed.append("%s（回滚异常：%r）" % (_t(key), e))

    def _inverse(self, key, old):
        if key.startswith("bind_"):
            return {"op": "set_bind", "gesture": key[5:], "action": old}
        if key == "gain":
            return {"op": "set_gain", "value": old}
        if key == "natural":
            return {"op": "set_natural", "value": old}
        if key == "hold_ms":
            return {"op": "set_hold_ms", "value": old}
        if key == "rc_hold_ms":
            return {"op": "set_rc_hold", "value": old}
        if key == "input_mode":
            return {"op": "set_input_mode", "value": old}
        if key == "edid_correct":
            return {"op": "set_edid_correct", "value": bool(old)}
        raise ValueError("无法回滚未知键 %r" % (key,))

    # ------------------------------------------------------------ 历史 / 撤销
    def _journal(self, rec):
        try:
            os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log("AI 历史写入失败: %r" % (e,))

    def history(self, limit=50):
        out = []
        try:
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return out[-limit:]

    def undo_last(self):
        """撤销最近一次未被撤销的 apply。"""
        recs = self.history(limit=200)
        undone = {r.get("undo_of") for r in recs if r.get("kind") == "undo"}
        for rec in reversed(recs):
            if rec.get("kind") != "apply" or rec.get("result") != "ok":
                continue
            if rec.get("txn") in undone:
                continue
            ops = [self._inverse(k, v) for k, v in (rec.get("before") or {}).items()]
            if not ops:
                return None
            res = self.apply(ops, journal=False)
            self._journal({"kind": "undo", "undo_of": rec.get("txn"),
                           "ts": int(time.time()), "result": "ok" if res.ok else "fail",
                           "txn": res.txn})
            return res
        return None

    def clear_history(self):
        try:
            os.unlink(self.history_path)
        except OSError:
            pass


# ---------------------------------------------------------------- 离线自检

class _FakeApp(object):
    """模拟 dptouch 的 set_*：非法值静默 return、hold 绑定自动开 rc_hold。"""

    def __init__(self):
        self.cfg = {
            "bind_tap": "left", "bind_swipe": "scroll",
            "bind_hold_swipe": "drag", "bind_hold": "right",
            "gain": 1.0, "natural": "system", "hold_ms": 250,
            "rc_hold_ms": 800, "input_mode": "auto", "edid_correct": True,
        }
        self.calls = []

    def set_bind(self, gesture, action):
        if gesture not in P.GESTURES or action not in P.GESTURES[gesture]:
            return
        self.calls.append(("bind", gesture, action))
        self.cfg["bind_" + gesture] = action
        if gesture == "hold" and action != "none" and not int(self.cfg.get("rc_hold_ms") or 0):
            self.cfg["rc_hold_ms"] = 800

    def set_gain(self, g):
        if not any(abs(float(g) - x) < 1e-9 for x in P.GAINS):
            return
        self.calls.append(("gain", g))
        self.cfg["gain"] = float(g)

    def set_natural(self, k):
        if k not in P.NATURAL:
            return
        self.calls.append(("natural", k))
        self.cfg["natural"] = k

    def set_hold_ms(self, ms):
        if ms not in P.HOLD_MS:
            return
        self.calls.append(("hold", ms))
        self.cfg["hold_ms"] = ms

    def set_rc_hold(self, ms):
        if ms not in P.RC_HOLD_MS:
            return
        self.calls.append(("rc", ms))
        self.cfg["rc_hold_ms"] = ms

    def set_input_mode(self, m):
        if m not in P.INPUT_MODES:
            return
        self.calls.append(("input", m))
        self.cfg["input_mode"] = m

    def set_edid_correct(self, on):
        self.calls.append(("edid", on))
        self.cfg["edid_correct"] = bool(on)


def selftest_offline():
    import tempfile
    ok = 0
    bad = []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    with tempfile.TemporaryDirectory() as td:
        app = _FakeApp()
        br = AISettingsBridge(app, history_path=os.path.join(td, "h.jsonl"))

        ops = [{"op": "set_bind", "gesture": "tap", "action": "right"}]
        ch = br.preview(ops)
        ck("预览一条", len(ch) == 1 and ch[0].old == "left" and ch[0].new == "right")
        ck("预览不改状态", app.cfg["bind_tap"] == "left")

        r = br.apply(ops)
        ck("执行成功", r.ok and app.cfg["bind_tap"] == "right", str(r.errors))
        ck("验证记录", len(r.applied) == 1)
        ck("历史落盘", len(br.history()) == 1)

        # 入口强校验：非法值 / 未知字段 / 未知操作，preview 阶段就拒绝
        for bad_ops in ([{"op": "set_gain", "value": 3.0}],
                        [{"op": "set_bind", "gesture": "tap", "action": "nope"}],
                        [{"op": "set_gain", "value": 1.5, "extra": 1}],
                        [{"op": "rm_rf"}],
                        [{"op": "set_edid_correct", "value": "yes"}],
                        []):
            try:
                br.preview(bad_ops)
                bad.append("非法操作未被拒绝：%r" % (bad_ops,))
            except P.ProtocolError:
                ok += 1
        ck("拒绝后状态未变", app.cfg["gain"] == 1.0)

        try:
            br.apply([{"op": "set_gain", "value": 3.0}])
            bad.append("apply 未拒绝非法 gain")
        except P.ProtocolError:
            ok += 1
        ck("apply 拒绝后未落盘", len(br.history()) == 1)

        # hold 绑定的隐含副作用
        app.cfg["rc_hold_ms"] = 0
        ch2 = br.preview([{"op": "set_bind", "gesture": "hold", "action": "right"}])
        ck("隐含 rc_hold 变更", any(c.implicit and c.key == "rc_hold_ms" for c in ch2))
        r3 = br.apply([{"op": "set_bind", "gesture": "hold", "action": "right"}])
        ck("隐含副作用生效", r3.ok and app.cfg["rc_hold_ms"] == 800)

        # 撤销
        before = dict(app.cfg)
        r4 = br.apply([{"op": "set_gain", "value": 2.0}])
        ck("改速度", r4.ok and app.cfg["gain"] == 2.0)
        u = br.undo_last()
        ck("撤销成功", u is not None and u.ok and app.cfg["gain"] == 1.0,
           str(u.errors) if u else "none")
        u2 = br.undo_last()
        ck("撤销只撤一次", u2 is not None and u2.txn != u.txn)

        # 回滚：一项成功 + 一项必然失败（模拟 setter 静默）
        app.set_gain = lambda g: None          # 让 gain 永远不生效
        r5 = br.apply([{"op": "set_natural", "value": "on"},
                       {"op": "set_gain", "value": 1.5}])
        ck("部分失败整体失败", not r5.ok and r5.errors)
        ck("成功的项被回滚", app.cfg["natural"] == "system", app.cfg["natural"])
        ck("回滚项用标题", "滚动速度" in r5.rolled_back, r5.rolled_back)

        # 回滚不掉的项必须被点名（模拟：恢复 system 时 setter 静默吞掉）
        orig_nat = app.set_natural
        def broken_nat(k):
            if k == "off":
                return
            orig_nat(k)
        app.set_natural = broken_nat
        app.cfg["natural"] = "off"
        r6 = br.apply([{"op": "set_natural", "value": "on"},
                       {"op": "set_gain", "value": 1.5}])
        ck("回滚失败被报告", not r6.ok and any("滚动方向" in x for x in r6.rollback_failed),
           "%r / %r" % (r6.rollback_failed, r6.errors))

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    print(json.dumps(selftest_offline(), ensure_ascii=False))
