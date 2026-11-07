"""Single touch-routing policy. OS preference is not proof of native capability."""
import platform
import re

POLICIES = ('auto', 'native', 'legacy')
TITLES = ('自动', '原生', '兼容模拟')

def modern(version=None):
    parts = re.findall(r'\d+', platform.mac_ver()[0] if version is None else version)
    return tuple((list(map(int, parts)) + [0, 0])[:2]) >= (27, 3)

def migrate(raw):
    """A stored boolean was also saved for defaults: provenance is unknowable.

    Neither true nor false proves a manual choice: both were written by normal
    saves after OS-default expansion. Do not pin users to a guessed manual mode.
    Modern legacy still requires fresh acknowledgement. New policy always wins.
    """
    if raw.get('touch_mode') in POLICIES:
        return raw['touch_mode']
    return 'auto'

def resolve(policy, touch_iface, version=None, legacy_ack=False):
    policy = policy if policy in POLICIES else 'auto'
    is_modern = modern(version)
    selected = ('pen_only' if not touch_iface else
                'native' if policy == 'native' or (policy == 'auto' and is_modern) else
                'native' if is_modern and not legacy_ack else 'legacy')
    reason = ('未检测到触摸接口 · 保留笔功能' if not touch_iface else
              'macOS 27.3+ · 优先原生，能力尚未验证' if policy == 'auto' and is_modern else
              '较早或未知系统 · 使用兼容模拟' if policy == 'auto' else
              '尚未确认关闭系统触控手势 · 暂用原生' if policy == 'legacy' and is_modern and not legacy_ack else
              '手动选择 · 已确认关闭系统触控手势' if policy == 'legacy' and is_modern else '手动选择')
    return dict(touch_mode=policy, selected_mode=selected, mode_reason=reason,
                native_touch=selected == 'native',
                native_hint='请在系统设置开启触控手势；无事件不会自动切回模拟' if selected == 'native' else '')
