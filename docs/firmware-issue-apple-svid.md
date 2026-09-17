# 固件问题报告：DP-in 外接 Mac 时，平板仅输出笔、不输出触摸屏

> 一句话：**平板的 USB 服务 `usb_dp_relay` 通过 DP Alt Mode 的 SVID 认出主机是 Mac 后，
> 主动只创建 Pen-only 的 HID 接口，不创建触摸屏接口。**
> 这个判断发生在 USB 接口创建之前，因此 Mac 侧任何软件（应用、驱动、协议栈）都无法修复。

---

## 1. 环境

| 项目 | 值 |
|---|---|
| 平板 | 小米平板 9 Pro Max（型号 M367FC / product `yingtian`，序列号 `0585D89009983540`） |
| 主机 | Mac mini M4（Mac16,10），macOS 27.0（Build 26A428） |
| 连接 | 一根 USB-C 转 USB-C 全功能线，直连主机本体 Type-C 口（`atc3`） |
| 形态 | 平板 DP-in 作为 Mac 的外接显示器（主显示器，面板 `MI DISPLAY`，3408×2272） |
| 采集 | Google 官方 platform-tools 37.0.1，`adb logcat -b all -v threadtime` |

## 2. 设备侧日志原文（决定性证据）

时间 `2026-09-17 15:00:49`，同一秒内、全程 **8 毫秒**（`logcat` 原文，进程 31218/22539，TAG `usb_dp_relay`）：

```
15:00:49.226 usb_dp_relay: adapter_svid: 1452
15:00:49.227 usb_dp_relay: host type: Mac
15:00:49.227 usb_dp_relay: setup (UDC:cc500000.dwc3) is_mac=1 first=1...
15:00:49.234 usb_dp_relay: hid.usb0 desc: 133 bytes
15:00:49.234 usb_dp_relay: hid.usb1 desc: 107 bytes
```

换算与含义：

- `adapter_svid: 1452` —— 十进制 **1452 = 0x05AC = Apple 的 USB VID**，即 DP Alt Mode 的
  SVID 上报 Apple。**这是「平板看主机」方向的标识**，与平板自己的 USB VID 无关。
- `host type: Mac` / `is_mac=1` —— 平板据此判定主机为 Mac。
- `hid.usb0 desc: 133 bytes` —— 键鼠接口（Keyboard + Mouse），与主机无关时也建。
- `hid.usb1 desc: 107 bytes` —— **笔接口**。Mac 分支下 107 字节 = 只有 Pen 集合
  （UsagePage `0x0D` / Usage `0x02`），**没有** TouchScreen 集合（`0x0D` / `0x04`）。

## 3. 对照：同一块平板接 Windows

| | Windows 主机 | Mac 主机 |
|---|---|---|
| `MI_00` | WPD（MTP 语义） | MTP |
| `MI_01` | Keyboard + Mouse | 133 B（Keyboard + Mouse） |
| `MI_02` | **TouchScreen(`0x0D:0x04`) + Pen(`0x0D:0x02`)** | **仅 Pen（107 B）** |
| 手指触控 | 免驱即用 | 完全无反应 |

Windows 侧判据（`xiaomi-pad-touch-diag.md`）：`HID_DEVICE_UP:000D_U:0004` 存在。
两侧的 `VID/PID` 与 HID 描述符差异，说明**是同一套硬件按主机身份走了不同分支**，而不是线材/速率/协议栈问题。

## 4. 为什么主机侧软件修不了

`usb_dp_relay` 在 `setup (UDC:cc500000.dwc3)` 阶段（即 UDC 绑定、接口创建之前）就完成了
`is_mac` 判定，并按结果选择描述符。等 macOS 枚举到设备时，触摸屏接口**从来没有被创建**——
主机能看到的 HID 集合里，除笔以外的所有候选都为 0（全机 29 个 HID 设备中 `0x0D:0x04` 命中 0）。

主机侧所有能想到的手段（设备描述符指纹、MS OS 描述符探测、`SET_IDLE`、用户态补发控制请求、
IOHIDFamily 过滤绕过）均无法改变一个不存在的接口。可用的只剩「平板固件增加 Mac 分支的触摸输出」这一条。

## 5. 建议修复（供小米评估，二选一即可）

1. **Mac 分支同样输出 Touch + Pen 描述符**（与 Windows 分支一致）。最小改动量：
   `if (is_mac) { use_pen_only_descriptor(); }` 里的分支收敛成同一套描述符。
   风险提示：Mac 会把多触点接口当成触摸屏设备，需要保证 Report 描述符符合 HID 触摸规范
   （本机型 Windows 分支已在用：`Report ID 1`，`10 × [Flags, ContactID, X16, Y16]` + `ContactCount`）。
2. **在系统设置里增加开关**：「DP-in 时向 Mac 输出手指触控」（默认关，用户自选）。
   好处是零风险、可回退。

## 6. 复现步骤（供工程师自证）

```bash
# 平板: 开发者选项 → 无线调试; Mac 侧用官方 platform-tools
adb logcat -c
# 平板退出 DP-in → 拔线 → 重新 C2C 连 Mac → 进入 DP-in → 等笔可用 → 手指划几下
adb logcat -b all -d -v threadtime | grep -E 'usb_dp_relay|adapter_svid|host type|is_mac|hid\.usb'
```

预期即可看到第 2 节的 5 行。

**交叉验证（在 Windows 笔记本上做同一件事）**：把上面最后一条换成
`adb logcat -b all -d -v threadtime | grep -E 'usb_dp_relay|adapter_svid|host type|is_mac|hid\.usb'`，
预期出现 `host type: Windows` / `is_mac=0` / `hid.usb1 desc: 914 bytes`（触摸 + 笔）。
注意：ADB 在 DP-in 下会因 `disableNetworking` 断开，所以必须**先拔线、用 A→C/数据线重连 ADB 后读缓冲日志**。

## 7. 影响面

- 仅影响 **DP-in 外接 Mac** 这一种形态；平板自身触控、接 Windows/Linux 主机均正常。
- 与 macOS 版本、Mac 机型、线材无关（同一根官方线在 Windows 上手指可用，已实测双向反证）。
- 主机侧应用只能承担「笔可用」这一半，无法承担「手指可用」。
