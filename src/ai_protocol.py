#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 输出的严格协议层。

只做三件事：从模型回复里取出第一个配平的 JSON 对象、校验顶层结构、
把 operations 规范成白名单内的结构化指令。**不做任何求值**：
没有 eval / exec / import，没有按名字 getattr 调用。

协议（P1）::

    {"type": "settings_change", "summary": "...", "operations": [
        {"op": "set_bind", "gesture": "tap", "action": "right"}
    ]}
    {"type": "answer", "text": "..."}
    {"type": "unsupported", "reason": "...", "why": "..."}

未知识别符、多余字段、类型不符、枚举越界、条数/长度超限 → 整份拒绝
（不做部分执行：用户看到的要么是完整方案，要么是明确失败）。
"""

import json

MAX_BYTES = 64 * 1024
MAX_OPS = 8
MAX_TEXT = 4000

# 与 hid_bridge.BIND_GESTURES / BIND_ACTIONS 保持一致（自检会比对）
GESTURES = {
    "tap": ("none", "left", "right", "middle", "double", "space", "back", "screen"),
    "swipe": ("none", "scroll", "drag", "right", "middle", "space", "back", "screen"),
    "hold_swipe": ("none", "drag", "right", "middle", "space", "back", "screen"),
    "hold": ("none", "right", "left", "middle", "double", "space", "back", "screen"),
}
GESTURE_KEYS = tuple(GESTURES)

# 与 dptouch_engine.NATURAL_OPTS / GAINS 保持一致
NATURAL = ("system", "on", "off")
GAINS = (0.5, 0.75, 1.0, 1.5, 2.0)
HOLD_MS = (200, 250, 350, 500)
RC_HOLD_MS = (0, 600, 800, 1000)
INPUT_MODES = ("auto", "pen", "touch")

TOP_TYPES = ("settings_change", "answer", "unsupported")


class ProtocolError(Exception):
    """模型输出不符合协议（带可读理由）。"""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


def extract_json(text):
    """从回复里取第一个配平的 JSON 对象（允许前后有说明文字/代码围栏）。"""
    if not isinstance(text, str):
        raise ProtocolError("模型回复不是文本")
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise ProtocolError("模型回复超过 %d KB" % (MAX_BYTES >> 10))
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    raise ProtocolError("回复里没有可解析的 JSON 对象")


def _need(obj, key, what="字段"):
    if key not in obj:
        raise ProtocolError("缺少%s %r" % (what, key))
    return obj[key]


def _check_keys(obj, allowed, where):
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ProtocolError("%s 里有未定义字段：%s" % (where, ", ".join(extra)))


def validate_op(op):
    """校验并规范化一条 operation；返回新 dict。"""
    if not isinstance(op, dict):
        raise ProtocolError("operation 必须是对象")
    name = op.get("op")
    if not isinstance(name, str):
        raise ProtocolError("operation 缺少 op")
    if name == "set_bind":
        _check_keys(op, ("op", "gesture", "action"), "set_bind")
        g = _need(op, "gesture")
        a = _need(op, "action")
        if g not in GESTURES:
            raise ProtocolError("未知手势：%r" % (g,))
        if a not in GESTURES[g]:
            raise ProtocolError("手势 %s 不支持动作 %r" % (g, a))
        return {"op": name, "gesture": g, "action": a}
    if name == "set_gain":
        _check_keys(op, ("op", "value"), "set_gain")
        v = _need(op, "value")
        if not _is_num(v) or not any(abs(float(v) - x) < 1e-9 for x in GAINS):
            raise ProtocolError("滚动速度只能是 %s" % (", ".join("%g" % x for x in GAINS),))
        return {"op": name, "value": float(v)}
    if name == "set_natural":
        _check_keys(op, ("op", "value"), "set_natural")
        v = _need(op, "value")
        if v not in NATURAL:
            raise ProtocolError("滚动方向只能是 %s" % (", ".join(NATURAL),))
        return {"op": name, "value": v}
    if name == "set_hold_ms":
        _check_keys(op, ("op", "value"), "set_hold_ms")
        v = _need(op, "value")
        if not _is_int(v) or v not in HOLD_MS:
            raise ProtocolError("「停住再滑」只能是 %s" % (", ".join(str(x) for x in HOLD_MS),))
        return {"op": name, "value": v}
    if name == "set_rc_hold":
        _check_keys(op, ("op", "value"), "set_rc_hold")
        v = _need(op, "value")
        if not _is_int(v) or v not in RC_HOLD_MS:
            raise ProtocolError("「停住不动」只能是 %s" % (", ".join(str(x) for x in RC_HOLD_MS),))
        return {"op": name, "value": v}
    if name == "set_input_mode":
        _check_keys(op, ("op", "value"), "set_input_mode")
        v = _need(op, "value")
        if v not in INPUT_MODES:
            raise ProtocolError("输入模式只能是 %s" % (", ".join(INPUT_MODES),))
        return {"op": name, "value": v}
    if name == "set_edid_correct":
        _check_keys(op, ("op", "value"), "set_edid_correct")
        v = _need(op, "value")
        if not isinstance(v, bool):
            raise ProtocolError("位置校正只能是 true / false")
        return {"op": name, "value": v}
    raise ProtocolError("不支持的操作：%r" % (name,))


def parse_intent(text):
    """解析模型回复 → dict(type=..., ...)。失败抛 ProtocolError。"""
    obj = extract_json(text)
    if not isinstance(obj, dict):
        raise ProtocolError("顶层必须是对象")
    typ = obj.get("type")
    if typ not in TOP_TYPES:
        raise ProtocolError("未知回复类型：%r" % (typ,))
    if typ == "answer":
        _check_keys(obj, ("type", "text"), "answer")
        t = obj.get("text")
        if not isinstance(t, str) or not t.strip():
            raise ProtocolError("answer 缺少 text")
        return {"type": "answer", "text": t.strip()[:MAX_TEXT]}
    if typ == "unsupported":
        _check_keys(obj, ("type", "reason", "why"), "unsupported")
        r = obj.get("reason")
        if not isinstance(r, str) or not r.strip():
            raise ProtocolError("unsupported 缺少 reason")
        why = obj.get("why") or ""
        if not isinstance(why, str):
            raise ProtocolError("why 必须是文本")
        return {"type": "unsupported", "reason": r.strip()[:MAX_TEXT],
                "why": why.strip()[:MAX_TEXT]}
    # settings_change
    _check_keys(obj, ("type", "summary", "operations"), "settings_change")
    ops = obj.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ProtocolError("settings_change 缺少 operations")
    if len(ops) > MAX_OPS:
        raise ProtocolError("一次最多改 %d 项" % MAX_OPS)
    out = [validate_op(o) for o in ops]
    summary = obj.get("summary") or ""
    if not isinstance(summary, str):
        raise ProtocolError("summary 必须是文本")
    return {"type": "settings_change", "summary": summary.strip()[:500],
            "operations": out}


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

    def fails(fn, *a):
        try:
            fn(*a)
            return False
        except ProtocolError:
            return True

    ck("解析纯 JSON", parse_intent('{"type":"answer","text":"hi"}')["text"] == "hi")
    ck("解析带围栏", parse_intent('说明\n```json\n{"type":"answer","text":"ok"}\n```')["text"] == "ok")
    ck("解析前后缀", parse_intent('先这样 {"type":"answer","text":"ok"} 完了')["type"] == "answer")
    ck("set_bind 合法", parse_intent(
        '{"type":"settings_change","operations":[{"op":"set_bind","gesture":"tap","action":"right"}]}'
    )["operations"][0]["action"] == "right")
    ck("未知类型被拒", fails(parse_intent, '{"type":"shell"}'))
    ck("未知 op 被拒", fails(parse_intent,
        '{"type":"settings_change","operations":[{"op":"eval","code":"1"}]}'))
    ck("多余字段被拒", fails(parse_intent,
        '{"type":"settings_change","operations":[{"op":"set_gain","value":1,"evil":1}]}'))
    ck("手势不匹配动作被拒", fails(parse_intent,
        '{"type":"settings_change","operations":[{"op":"set_bind","gesture":"tap","action":"scroll"}]}'))
    ck("bool 假冒数字被拒", fails(parse_intent,
        '{"type":"settings_change","operations":[{"op":"set_hold_ms","value":true}]}'))
    ck("档位越界被拒", fails(parse_intent,
        '{"type":"settings_change","operations":[{"op":"set_gain","value":3}]}'))
    ck("条数超限被拒", fails(parse_intent, json.dumps(
        {"type": "settings_change", "operations": [{"op": "set_edid_correct", "value": True}] * 9})))
    ck("无 JSON 被拒", fails(parse_intent, "我不会"))
    ck("空 answer 被拒", fails(parse_intent, '{"type":"answer","text":"  "}'))

    # 与真实常量比对（防止两边漂移）
    try:
        import hid_bridge as HB
        real = {g: tuple(o) for g, _t, o, _d in HB.BIND_GESTURES}
        ck("手势表与 hid_bridge 一致", real == GESTURES, str(real.keys()))
        ck("档位与 hid_bridge 一致",
           tuple(HB.HOLD_MS_OPTS) == HOLD_MS and tuple(HB.RC_HOLD_MS_OPTS) == RC_HOLD_MS)
        ck("输入模式一致", tuple(HB.INPUT_MODES) == INPUT_MODES)
    except Exception as e:
        bad.append("常量比对失败 %r" % (e,))
    try:
        import dptouch_engine as E
        ck("滚动档位与引擎一致", tuple(float(v) for v, _ in E.GAINS) == GAINS)
        ck("滚动方向与引擎一致", tuple(E.NATURAL_OPTS) == NATURAL)
    except Exception as e:
        bad.append("引擎常量比对失败 %r" % (e,))

    return {"ok": not bad, "passed": ok, "fail": bad}


if __name__ == "__main__":
    print(json.dumps(selftest_offline(), ensure_ascii=False))
