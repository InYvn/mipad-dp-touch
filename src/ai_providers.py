#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Provider 适配层（P1：OpenAI 兼容 / OpenRouter / Ollama / Gemini）。

约束：
* 只用标准库（App 不额外装包）；
* 不做流式（P1 用一次性响应，简单可控；超时/取消语义清晰）；
* 密钥只在内存里，错误信息经 ai_keychain.redact；
* 返回统一的 Reply(text, usage, raw)，调用方只认 text + usage。

OpenAI 兼容口覆盖：OpenAI、OpenRouter、Ollama（/v1）、vLLM、LM Studio 等。
"""

import json
import socket
import urllib.error
import urllib.request

import ai_keychain as KC


class ProviderError(Exception):
    def __init__(self, kind, message, status=0, retry_after=None):
        super(ProviderError, self).__init__(message)
        self.kind = kind          # config|auth|rate_limit|timeout|network|bad_response
        self.status = status
        self.retry_after = retry_after

    def user_text(self):
        return {
            "config": "配置不完整",
            "auth": "密钥无效或没有权限",
            "rate_limit": "请求太频繁或额度用完了",
            "timeout": "请求超时",
            "network": "网络连接失败",
            "bad_response": "模型返回的内容无法解析",
        }.get(self.kind, "请求失败") + ("：%s" % self.args[0] if self.args and self.args[0] else "")


PROVIDERS = {
    "openai": {
        "title": "OpenAI 兼容", "kind": "openai",
        "base": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "keyless": False,
        "hint": "填任意 OpenAI 兼容服务的 Base URL 和模型名即可",
    },
    "openrouter": {
        "title": "OpenRouter", "kind": "openai",
        "base": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini",
        "keyless": False, "hint": "OpenRouter 的模型名要带厂商前缀",
    },
    "ollama": {
        "title": "Ollama（本机）", "kind": "openai",
        "base": "http://127.0.0.1:11434/v1", "model": "llama3.1",
        "keyless": True, "hint": "本机 Ollama，不需要密钥",
    },
    "gemini": {
        "title": "Google Gemini", "kind": "gemini",
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.0-flash", "keyless": False, "hint": "",
    },
}

DEFAULT_PROVIDER = "openai"


class Reply(object):
    def __init__(self, text, usage=None, raw=None):
        self.text = text
        self.usage = usage or {}
        self.raw = raw


def provider_ids():
    return list(PROVIDERS)


def meta(pid):
    return dict(PROVIDERS[pid])


def _post(url, payload, headers, timeout, cancel=None, accept_json=True):
    if cancel is not None and cancel.is_set():
        raise ProviderError("timeout", "已取消")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "MipadDPTouch-AI",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if e.code in (401, 403):
            raise ProviderError("auth", KC.redact(detail or ("HTTP %d" % e.code)), e.code)
        if e.code == 429:
            raise ProviderError("rate_limit",
                                KC.redact(detail or "HTTP 429"), e.code,
                                e.headers.get("Retry-After"))
        raise ProviderError("network", KC.redact("HTTP %d %s" % (e.code, detail)), e.code)
    except socket.timeout:
        raise ProviderError("timeout", "连接或读取超时")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            raise ProviderError("timeout", "连接或读取超时")
        raise ProviderError("network", KC.redact(str(e.reason)))
    except Exception as e:
        raise ProviderError("network", KC.redact(repr(e)))
    if cancel is not None and cancel.is_set():
        raise ProviderError("timeout", "已取消")
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        raise ProviderError("bad_response", "返回不是 JSON")
    if not isinstance(obj, dict):
        raise ProviderError("bad_response", "返回结构不对")
    return obj


def _openai_chat(base, key, model, messages, timeout, cancel, json_mode=True):
    url = base.rstrip("/") + "/chat/completions"
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": messages, "temperature": 0.2,
               "max_tokens": 1200}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        obj = _post(url, payload, headers, timeout, cancel)
    except ProviderError as e:
        # 有些兼容端点不认 response_format：去掉再试一次（只对 400 重试）
        if json_mode and e.status == 400:
            payload.pop("response_format", None)
            obj = _post(url, payload, headers, timeout, cancel)
        else:
            raise
    try:
        choice = (obj.get("choices") or [])[0]
        text = (choice.get("message") or {}).get("content") or ""
    except Exception:
        raise ProviderError("bad_response", "返回里没有 choices[0].message.content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("bad_response", "模型返回了空内容")
    return Reply(text, obj.get("usage") or {}, obj)


def _gemini_chat(base, key, model, messages, timeout, cancel):
    if not key:
        raise ProviderError("config", "Gemini 需要 API Key")
    url = "%s/models/%s:generateContent?key=%s" % (base.rstrip("/"), model, key)
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(m.get("content") or "")}]})
    payload = {"contents": contents,
               "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}}
    obj = _post(url, payload, {}, timeout, cancel)
    try:
        cand = (obj.get("candidates") or [])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts)
    except Exception:
        raise ProviderError("bad_response", "返回里没有 candidates[0].content.parts")
    if not text.strip():
        raise ProviderError("bad_response", "模型返回了空内容")
    return Reply(text, obj.get("usageMetadata") or {}, obj)


def chat(pid, key, base_url, model, messages, timeout=120, cancel=None, json_mode=True):
    """统一入口；失败抛 ProviderError。"""
    if pid not in PROVIDERS:
        raise ProviderError("config", "未知 provider：%r" % (pid,))
    meta_ = PROVIDERS[pid]
    base = (base_url or meta_["base"]).strip()
    model = (model or meta_["model"]).strip()
    if not base or not model:
        raise ProviderError("config", "Base URL / 模型名不能为空")
    if not meta_["keyless"] and not key:
        raise ProviderError("config", "还没有填 API Key")
    if meta_["kind"] == "gemini":
        return _gemini_chat(base, key, model, messages, timeout, cancel)
    return _openai_chat(base, key, model, messages, timeout, cancel, json_mode)


# ---------------------------------------------------------------- 离线自检

def selftest_offline():
    """用本机假 HTTP 服务跑一遍关键分支（不联网）。"""
    import http.server
    import tempfile
    import threading

    ok = 0
    bad = []

    def ck(name, cond, extra=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append("%s %s" % (name, extra))

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
            if "/slow" in self.path:
                import time
                time.sleep(1.5)
            if "/401" in self.path:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"bad key sk-abcdefghijklmnop"}')
                return
            if "/429" in self.path:
                self.send_response(429)
                self.send_header("Retry-After", "7")
                self.end_headers()
                self.wfile.write(b'{"error":"rate"}')
                return
            if "/bad" in self.path:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'not json')
                return
            text = '{"type":"answer","text":"hi"}'
            out = {"choices": [{"message": {"content": text}}],
                   "usage": {"total_tokens": 12}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d/v1" % port
    msgs = [{"role": "user", "content": "x"}]
    try:
        r = chat("openai", "sk-test", base, "m", msgs, timeout=5)
        ck("正常返回", r.text.startswith("{") and r.usage.get("total_tokens") == 12)

        try:
            chat("openai", "sk-test", base + "/401", "m", msgs, timeout=5)
            ck("401 → auth", False)
        except ProviderError as e:
            ck("401 → auth", e.kind == "auth")
            ck("401 错误已脱敏", "sk-abcdefghijklmnop" not in str(e))

        try:
            chat("openai", "sk-test", base + "/429", "m", msgs, timeout=5)
            ck("429 → rate_limit", False)
        except ProviderError as e:
            ck("429 → rate_limit", e.kind == "rate_limit" and e.retry_after == "7")

        try:
            chat("openai", "sk-test", base + "/bad", "m", msgs, timeout=5)
            ck("坏 JSON → bad_response", False)
        except ProviderError as e:
            ck("坏 JSON → bad_response", e.kind == "bad_response")

        try:
            chat("openai", "sk-test", "http://127.0.0.1:1/v1", "m", msgs, timeout=2)
            ck("连接失败 → network", False)
        except ProviderError as e:
            ck("连接失败 → network", e.kind == "network")

        try:
            chat("openai", "", base, "m", msgs, timeout=5)
            ck("缺 key → config", False)
        except ProviderError as e:
            ck("缺 key → config", e.kind == "config")

        try:
            chat("ollama", "", base, "m", msgs, timeout=5)
            ck("keyless 可用", True)
        except ProviderError as e:
            ck("keyless 可用", False, str(e))
    finally:
        srv.shutdown()
        srv.server_close()

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    print(json.dumps(selftest_offline(), ensure_ascii=False))
