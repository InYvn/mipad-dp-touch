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


def resolve(policy, touch_iface, version=None, legacy_ack=False, native_confirmed=False):
    """选路: 只看**手动策略 + 实测接口 + 已确认的原生能力**, 不拿系统版本当能力证明.

    背景 (本机实测, macOS 27.3): 系统虽然按版本「优先原生」, 但模块 HID 给的手指
    事件在内核层被丢成 no-op (`createEventForDigitizerCollection generates null
    event`), 手指只有 App 的「兼容模拟」能派发。把版本当成能力证明会让 auto 在
    27.3+ 盲选原生 = 手指完全没反应。所以 auto 的原生分支必须等 native_confirmed
    (用户/代码明确确认过本机原生可用)。

    policy          持久化策略: auto | native | legacy
    touch_iface     是否识别到手指接口 (False -> 仅笔, 与策略无关)
    version         macOS 版本字符串, 默认 platform.mac_ver()
    legacy_ack      用户是否确认过「关闭系统触控手势」(现代系统上用兼容模拟的前提)
    native_confirmed 是否确认过原生触控在本机真的可用 (默认 False)
    """
    policy = policy if policy in POLICIES else 'auto'
    is_modern = modern(version)
    if not touch_iface:
        selected = 'pen_only'
    elif policy == 'native':
        selected = 'native'                       # 用户手选: 明确要原生, 不接管
    elif policy == 'legacy':
        # 兼容模拟必须做过确认, 否则回落原生 (老规则: 宁可不模拟, 也不双发)
        selected = 'legacy' if (not is_modern or legacy_ack) else 'native'
    elif is_modern and native_confirmed:
        selected = 'native'                       # 自动 + 已确认原生可用
    else:
        # 自动 + 能力未确认 (含 27.3+ 的全部默认情况) / 老系统 -> 兼容模拟
        selected = 'legacy'
    if not touch_iface:
        reason = '未检测到触摸接口 · 保留笔功能'
    elif policy == 'native':
        reason = '手动选择 · 原生触控'
    elif policy == 'legacy' and is_modern and not legacy_ack:
        reason = '尚未确认关闭系统触控手势 · 暂用原生'
    elif policy == 'legacy':
        reason = ('手动选择 · 兼容模拟 · 已确认关闭系统触控手势' if is_modern
                  else '手动选择 · 兼容模拟')
    elif is_modern and native_confirmed:
        reason = 'macOS 27.3+ · 已确认原生可用 · 自动选择原生'
    elif is_modern:
        reason = 'macOS 27.3+ · 原生触控尚未验证 · 自动使用兼容模拟'
    else:
        reason = '较早或未知系统 · 使用兼容模拟'
    if selected == 'native':
        hint = ('请在系统设置开启触控手势；无事件不会自动切回模拟')
    elif is_modern and policy == 'auto' and not native_confirmed:
        hint = ('本机原生触控能力未经确认（本版本实测 27.3 原生为 no-op）；'
                '如需原生请在「触控与位置」手选「原生触控」，或在配置里写 native_confirmed=true')
    else:
        hint = ''
    return dict(touch_mode=policy, selected_mode=selected, mode_reason=reason,
                native_touch=selected == 'native', native_hint=hint)
