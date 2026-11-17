#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 许愿工坊的控制器（P1：只做设置修改）。

职责：拼上下文、跑后台请求、解析协议、生成预览、执行/撤销、维护对话。
不碰 AppKit：UI 通过 `revision` 轮询（主线程 tick），线程只改状态。

状态机::

    idle → thinking → proposed → applying → idle
                 ↘ error

* `thinking` 时只允许「停止」；
* `proposed` 时只允许「执行 / 取消」；
* 任何请求都不在主线程序列化/发送。
"""

import json
import os
import threading
import time

import ai_keychain as KC
import ai_protocol as P
import ai_providers as PR

DEFAULTS = {
    "provider": PR.DEFAULT_PROVIDER,
    "base_url": "",
    "model": "",
    "timeout": 120,
    "show_payload": False,
}

MAX_HISTORY = 40          # 发给模型的历史条数
MAX_STORED = 60           # 本地保存的对话条数


def _now():
    return time.strftime("%H:%M:%S")


class AIController(object):
    def __init__(self, app, bridge, log=None, chat_path=None, keychain=KC):
        self.app = app
        self.bridge = bridge
        self._log = log or (lambda _m: None)
        self.keychain = keychain
        self.chat_path = chat_path or os.path.expanduser(
            "~/Library/Application Support/MipadDPTouch/ai_chat.json")
        self.state = "idle"          # idle|thinking|proposed|applying|error
        self.messages = []           # [{role,text,ts,kind}]
        self.pending = None          # 解析后的 intent
        self.changes = []            # [Change]
        self.error = ""
        self.revision = 0
        self._thread = None
        self._cancel = threading.Event()
        self._load_chat()

    # ------------------------------------------------------------ 设置
    def settings(self):
        raw = (self.app.cfg.get("ai") or {}) if hasattr(self.app, "cfg") else {}
        out = dict(DEFAULTS)
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
        out["provider"] = out["provider"] if out["provider"] in PR.PROVIDERS \
            else PR.DEFAULT_PROVIDER
        try:
            out["timeout"] = max(10, min(600, int(out["timeout"])))
        except Exception:
            out["timeout"] = DEFAULTS["timeout"]
        return out

    def save_settings(self, **kw):
        cur = self.settings()
        cur.update({k: v for k, v in kw.items() if k in DEFAULTS})
        self.app.cfg["ai"] = cur
        try:
            from dptouch import save_cfg
            save_cfg(self.app.cfg)
        except Exception as e:
            self._log("AI 设置保存失败: %r" % (e,))
        self._bump()

    def has_key(self, pid=None):
        pid = pid or self.settings()["provider"]
        if PR.PROVIDERS.get(pid, {}).get("keyless"):
            return True
        return bool(self.keychain.load(pid))

    def set_key(self, key, pid=None):
        pid = pid or self.settings()["provider"]
        self.keychain.save(pid, key)
        self._bump()

    def clear_key(self, pid=None):
        self.keychain.delete(pid or self.settings()["provider"])
        self._bump()

    def status_text(self):
        s = self.settings()
        title = PR.PROVIDERS.get(s["provider"], {}).get("title", s["provider"])
        model = s["model"] or PR.PROVIDERS.get(s["provider"], {}).get("model", "")
        if self.state == "thinking":
            return "正在思考…（%s · %s）" % (title, model)
        if not self.has_key():
            return "未配置密钥 · 点「AI 设置…」填 Key"
        if self.state == "error":
            return "出错 · %s" % (self.error or "未知错误")
        return "已就绪 · %s · %s" % (title, model)

    # ------------------------------------------------------------ 对话
    def _bump(self):
        self.revision += 1

    def _add(self, role, text, kind="text"):
        self.messages.append({"role": role, "text": text, "ts": _now(), "kind": kind})
        self.messages = self.messages[-MAX_STORED:]
        self._save_chat()
        self._bump()

    def _load_chat(self):
        try:
            with open(self.chat_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                self.messages = [m for m in d if isinstance(m, dict)][-MAX_STORED:]
        except Exception:
            self.messages = []

    def _save_chat(self):
        try:
            os.makedirs(os.path.dirname(self.chat_path), exist_ok=True)
            with open(self.chat_path, "w", encoding="utf-8") as f:
                json.dump(self.messages[-MAX_STORED:], f, ensure_ascii=False, indent=1)
        except Exception as e:
            self._log("AI 对话保存失败: %r" % (e,))

    def clear_chat(self):
        self.messages = []
        self.pending = None
        self.changes = []
        self.state = "idle"
        self._save_chat()
        self._bump()

    # ------------------------------------------------------------ 请求
    def _system_prompt(self):
        return (
            "你是 Mipad DP Touch 的设置助手。用户用自然语言描述想改的设置，"
            "你只能通过给定的 operations 修改，不能发明接口，不能声称改了实际没改的东西。\n\n"
            + self.bridge.context_text() +
            "\n\n如果用户的要求做不到（例如手指手势、系统级操作），"
            "必须回 unsupported 并说明原因，不要给假的替代方案。"
            "只输出一个 JSON 对象，不要 markdown 代码围栏，不要多余文字。"
        )

    def _history_for_model(self):
        out = []
        for m in self.messages[-MAX_HISTORY:]:
            if m.get("kind") == "error":
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            out.append({"role": role, "content": str(m.get("text") or "")[:2000]})
        return out

    def ask(self, text):
        text = (text or "").strip()
        if not text:
            return False
        if self.state == "thinking":
            return False
        s = self.settings()
        if not self.has_key(s["provider"]):
            self.error = "还没有配置 API Key"
            self.state = "error"
            self._add("system", "先在「AI 设置…」里填好 Provider 和 API Key。", "error")
            return False
        self._add("user", text)
        self.state = "thinking"
        self.error = ""
        self.pending = None
        self.changes = []
        self._cancel.clear()
        self._bump()
        self._thread = threading.Thread(target=self._run, args=(text, s), daemon=True)
        self._thread.start()
        return True

    def _run(self, text, s):
        msgs = [{"role": "system", "content": self._system_prompt()}]
        msgs += self._history_for_model()
        try:
            reply = PR.chat(s["provider"], self.keychain.load(s["provider"]),
                            s["base_url"], s["model"], msgs,
                            timeout=s["timeout"], cancel=self._cancel)
        except PR.ProviderError as e:
            if self._cancel.is_set():
                self.state = "idle"
            else:
                self.error = e.user_text()
                self.state = "error"
                self._add("system", "请求失败：%s" % self.error, "error")
            self._bump()
            return
        except Exception as e:
            self.error = KC.redact(repr(e))
            self.state = "error"
            self._add("system", "请求异常：%s" % self.error, "error")
            self._bump()
            return
        if self._cancel.is_set():
            self.state = "idle"
            self._bump()
            return
        try:
            self._handle_reply(reply.text)
        except Exception as e:
            self.error = KC.redact("处理回复失败：%r" % (e,))
            self.state = "error"
            self._add("system", self.error, "error")

    def _handle_reply(self, text):
        try:
            intent = P.parse_intent(text)
        except P.ProtocolError as e:
            self.error = "模型输出不符合协议：%s" % (e,)
            self.state = "error"
            self._add("system", self.error + "\n原始回复：" + str(text)[:600], "error")
            self._bump()
            return
        typ = intent["type"]
        if typ == "answer":
            self.state = "idle"
            self._add("assistant", intent["text"])
            return
        if typ == "unsupported":
            self.state = "idle"
            msg = intent["reason"]
            if intent.get("why"):
                msg += "\n" + intent["why"]
            self._add("assistant", msg)
            return
        # settings_change
        self.pending = intent
        self.changes = self.bridge.preview(intent["operations"])
        self.state = "proposed"
        lines = ["准备修改 %d 项：" % len(self.changes)]
        for c in self.changes:
            lines.append("· %s：%s → %s%s" % (
                c.title, self._fmt(c.old), self._fmt(c.new),
                ("（%s）" % c.warn) if c.warn else ""))
        self._add("assistant", "\n".join(lines) + "\n\n确认后点「执行改动」。", "proposal")
        self._bump()

    @staticmethod
    def _fmt(v):
        if isinstance(v, bool):
            return "开" if v else "关"
        if isinstance(v, float):
            return "%g" % v
        return str(v)

    def stop(self):
        """取消当前请求：线程不杀，结果丢弃。"""
        if self.state == "thinking":
            self._cancel.set()
            self.state = "idle"
            self._add("system", "已停止。")
        return True

    # ------------------------------------------------------------ 执行
    def execute(self):
        if self.state != "proposed" or not self.pending:
            return None
        ops = self.pending["operations"]
        self.state = "applying"
        self._bump()
        try:
            res = self.bridge.apply(ops)
        except Exception as e:
            res = None
            self.error = KC.redact("执行异常：%r" % (e,))
            self._add("system", self.error, "error")
        finally:
            self.pending = None
            self.changes = []
            self.state = "idle"
        if res is None:
            return None
        if res.ok:
            self._add("system", "已执行：%d 项设置已生效。" % len(res.applied), "done")
        else:
            msg = "执行失败：" + "；".join(res.errors or ["未知原因"])
            if res.rollback_failed:
                msg += "\n未能完全回滚：" + "；".join(res.rollback_failed)
            elif res.rolled_back:
                msg += "\n已回滚：%s" % "、".join(res.rolled_back)
            self._add("system", msg, "error")
        return res

    def discard(self):
        if self.state == "proposed":
            self.pending = None
            self.changes = []
            self.state = "idle"
            self._add("system", "已取消这次改动。")
        return True

    def undo(self):
        res = self.bridge.undo_last()
        if res is None:
            self._add("system", "没有可撤销的改动。", "error")
            return None
        if res.ok:
            self._add("system", "已撤销上一次改动。", "done")
        else:
            self._add("system", "撤销失败：" + "；".join(res.errors or ["未知原因"]), "error")
        return res

    # ------------------------------------------------------------ 线程观察
    def busy(self):
        return self.state in ("thinking", "applying")


# ---------------------------------------------------------------- 离线自检

def selftest_offline():
    """用假 Provider + 假 app 跑完整链路（不联网）。"""
    import tempfile

    import ai_settings as S

    ok = 0
    bad = []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    class KCStub(object):
        def __init__(self):
            self.k = {}

        def load(self, p):
            return self.k.get(p)

        def save(self, p, v):
            self.k[p] = v

        def delete(self, p):
            self.k.pop(p, None)

    with tempfile.TemporaryDirectory() as td:
        app = S._FakeApp()
        app.cfg["ai"] = {}
        bridge = S.AISettingsBridge(app, history_path=os.path.join(td, "h.jsonl"))
        kc = KCStub()
        ai = AIController(app, bridge, chat_path=os.path.join(td, "c.json"), keychain=kc)

        ck("未配置时提问被拒", ai.ask("把笔轻点改成右键") is False)
        kc.save("openai", "sk-x")
        ck("配置后允许", ai.has_key())

        import ai_providers as PR2
        orig = PR2.chat
        try:
            PR2.chat = lambda *a, **k: PR2.Reply(json.dumps({
                "type": "settings_change", "summary": "改轻点",
                "operations": [{"op": "set_bind", "gesture": "tap", "action": "right"}]}))
            ck("提问启动", ai.ask("把笔轻点改成右键"))
            ai._thread.join(5)
            ck("进入待确认", ai.state == "proposed" and len(ai.changes) == 1)
            r = ai.execute()
            ck("执行成功", r and r.ok and app.cfg["bind_tap"] == "right")
            ck("回到 idle", ai.state == "idle")

            PR2.chat = lambda *a, **k: PR2.Reply(json.dumps({
                "type": "unsupported", "reason": "手指手势由系统处理",
                "why": "原生触控不经过笔的桥"}))
            ai.ask("手指双击改成左键")
            ai._thread.join(5)
            ck("不支持如实回答", ai.state == "idle"
               and any("手指" in m["text"] for m in ai.messages))

            PR2.chat = lambda *a, **k: PR2.Reply("我不会")
            ai.ask("随便")
            ai._thread.join(5)
            ck("坏输出报错", ai.state == "error")

            PR2.chat = lambda *a, **k: (_ for _ in ()).throw(PR2.ProviderError("timeout", "超时"))
            ai.ask("再试")
            ai._thread.join(5)
            ck("Provider 错误可读", ai.state == "error" and "超时" in ai.error)
        finally:
            PR2.chat = orig

        # 对话持久化
        ai2 = AIController(app, bridge, chat_path=ai.chat_path, keychain=kc)
        ck("对话可恢复", len(ai2.messages) == len(ai.messages))

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    print(json.dumps(selftest_offline(), ensure_ascii=False))
