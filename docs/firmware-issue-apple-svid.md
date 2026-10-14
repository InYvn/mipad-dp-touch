# 固件问题报告：DP-in 外接 Mac 时，平板仅输出笔、不输出触摸屏

> 一句话：**平板的 USB 服务 `usb_dp_relay` 通过 DP Alt Mode 的 SVID 认出主机是 Mac 后，
> 主动只创建 Pen-only 的 HID 接口，不创建触摸屏接口。**
> 这个判断发生在 USB 接口创建之前，因此 Mac 侧任何软件（应用、驱动、协议栈）都无法修复。
>
> 本文件是这个问题**唯一的一份统一说明**：现象 → 官方文件位置与代码 → 证据 → 为什么主机侧修不了 →
> 给小米的建议 → 复现步骤 → 尚未证实的部分。**证据与推断严格分开**（推断集中在第 9 节）。

---

## 1. 环境

| 项目 | 值 |
|---|---|
| 平板 | 小米平板 9 Pro Max（型号 M367FC / product `yingtian`，序列号 `0585D89009983540`，SoC `xring_o3_asic`） |
| 主机 | Mac mini M4（Mac16,10），macOS 27.0（Build 26A428） |
| 连接 | 一根 USB-C 转 USB-C 全功能线，直连主机本体 Type-C 口（`atc3`） |
| 形态 | 平板 DP-in 作为 Mac 的外接显示器（主显示器，面板 `MI DISPLAY`，3408×2272） |
| 采集 | Google 官方 platform-tools 37.0.1；`adb logcat -b all -v threadtime`；设备端只读文件直读（2026-09-18） |

## 2. 证据一：设备侧日志原文（决定性）

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

完整调用链（同一秒，含 HAL 与 Java 两侧）：

```
09-17 15:00:49.224 27544 I DPIN    : DPIN_OneTrack track: dp_in_connect_event params={}
09-17 15:00:49.225 27544 I DPIN    : g Service connected
09-17 15:00:49.226 27544 I DPIN    : a Started successfully
09-17 15:00:49.226 27544 I DPIN    : OsdManager onMonitorModeChanged: true
09-17 15:00:49.226 31218 I usb_dp_relay_hal: prepareGadget()
09-17 15:00:49.226 31218 I usb_dp_relay_hal: tracking client pid 27544
09-17 15:00:49.226 31218 I usb_dp_relay: bridge_prepare() ready
09-17 15:00:49.226 31218 I usb_dp_relay: bridge_start()
09-17 15:00:49.226 31218 I usb_dp_relay: bridge thread started
09-17 15:00:49.226 27544 I DPIN    : g Gadget prepared
09-17 15:00:49.226 27544 I DPIN    : g status: enabled=false, mode=dp, gadgetReady=false
09-17 15:00:49.226 31218 I usb_dp_relay: adapter_svid: 1452
09-17 15:00:49.227 31218 I usb_dp_relay: host type: Mac
09-17 15:00:49.227 31218 I usb_dp_relay: setup (UDC:cc500000.dwc3) is_mac=1 first=1...
09-17 15:00:49.234 31218 I usb_dp_relay: hid.usb0 desc: 133 bytes
09-17 15:00:49.234 31218 I usb_dp_relay: hid.usb1 desc: 107 bytes
09-17 15:00:49.234 31218 I usb_dp_relay: symlinks created
```

更早的 Java 侧（`com.milink.service`，tag `DPIN`）：
`UsbBroadcastReceiver onReceive: miui.intent.action.ACTION_USB_DP_IN_STATE_CHANGED` →
`USB DP IN state transition: 0 -> 1` → `handleUsbAttachedWithCta()` →
`b isCtaAgreed() status=1, agreed=true` → `CTA already agreed, proceed with USB attach`。
该 APK 经 R8 混淆（方法名 a/b/c），Java 侧只剩日志可读 —— **Mac 判定不在 Java 侧**。

## 3. 证据二：官方代码与策略在哪（设备上绝对路径 + 逐字内容）

### 3.1 文件位置

| 路径 | 是什么 | shell 可读性 |
|---|---|---|
| `/odm/bin/vendor.xiaomi.usb.dp-service` | **官方逻辑本体**（HAL 二进制） | **root-only**：`ls` 见名，`stat/size/read` 全 Permission denied |
| `/odm/etc/init/vendor.xiaomi.usb.dp-service.rc` | 服务定义 | 644，可读 |
| `/odm/etc/vintf/manifest/vendor.xiaomi.usb.dp-service.xml` | VINTF 声明 + 源码树路径 | 可读 |
| `/vendor/etc/selinux/vendor_property_contexts:744-746` | 两个官方属性 + 源码树路径 | 可读 |
| `/vendor/etc/selinux/vendor_file_contexts:962` | 二进制 SELinux 上下文 | 可读 |
| `/vendor/etc/selinux/vendor_sepolicy.cil:25632-25806` | 官方 `.te` 逐行规则（带**官方源文件行号**） | 可读 |

**Xiaomi 自己源码树里的位置**（被上面两个文件的注释原样写着，可直接照抄到 AOSP 目录树）：

```
vendor/xiaomi/proprietary/touch/usb_dp_relay_aidl/
├── vendor.xiaomi.usb.dp-service.xml        ← VINTF（3.3）
└── sepolicy/
    ├── property_contexts                   ← 两个 persist 属性（3.4）
    └── hal_usb_dp_relay.te                 ← 规则（3.5，行号即此处行号）
```

### 3.2 `/odm/etc/init/vendor.xiaomi.usb.dp-service.rc`（全文）

```
service vendor.usb.dp.hal /odm/bin/vendor.xiaomi.usb.dp-service
    interface aidl vendor.xiaomi.usb.dp.IUsbDpRelay/default
    class hal
    user root
    group system input usb uhid
    disabled
    oneshot
    seclabel u:r:hal_usb_dp_relay_default:s0

on property:sys.boot_completed=1
    chmod 0666 /config/usb_gadget/g1/UDC
    chmod 0666 /config/usb_gadget/g1/idProduct
    chmod 0775 /config/usb_gadget/g1/configs/b.1
    chmod 0775 /config/usb_gadget/g1/functions
```

读法：`disabled + oneshot` = 平时不跑、按需拉起；`user root` 与 `group … input usb uhid` = 要动输入/USB/UHID；
末四条 chmod 说明它**直接操作 `/config/usb_gadget/g1`（gadget configfs）**。

### 3.3 `/odm/etc/vintf/manifest/vendor.xiaomi.usb.dp-service.xml`（全文）

```xml
<!--
    Input:
        vendor/xiaomi/proprietary/touch/usb_dp_relay_aidl/vendor.xiaomi.usb.dp-service.xml
-->
<manifest version="9.0" type="device">
    <hal format="aidl">
        <name>vendor.xiaomi.usb.dp</name>
        <fqname>IUsbDpRelay/default</fqname>
    </hal>
</manifest>
```

### 3.4 属性与文件的 SELinux 上下文

`/vendor/etc/selinux/vendor_property_contexts:744-746`：

```
#line 1 "vendor/xiaomi/proprietary/touch/usb_dp_relay_aidl/sepolicy/property_contexts"
persist.vendor.usb.dp_relay          u:object_r:vendor_usb_prop:s0
persist.vendor.usb.dp_relay_mode     u:object_r:vendor_usb_prop:s0
```

`/vendor/etc/selinux/vendor_file_contexts:962`：

```
/odm/bin/vendor\.xiaomi\.usb\.dp-service  u:object_r:hal_usb_dp_relay_default_exec:s0
```

### 3.5 规则：`vendor_sepolicy.cil:25632-25806`（`hal_usb_dp_relay.te`）

每段前的 `;;* lmx <行号> …/hal_usb_dp_relay.te` 就是官方源文件行号，等于把它干了什么摊开：

```
;;* lmx 7   (allow hal_usb_dp_relay_default hal_usb_dp_relay_default_exec (file (read getattr map execute open entrypoint)))
;;* lmx 10  (allow hal_usb_dp_relay_default servicemanager_202604 (binder (call transfer)))
;;* lmx 11  (allow hal_usb_dp_relay_default hal_usb_dp_relay_service (service_manager (add find)))      ← 注册 AIDL 服务
;;* lmx 14  (allow hal_usb_dp_relay_default vendor_usb_prop (property_service (set)))                  ← 写 persist.vendor.usb.dp_relay*
;;* lmx 14  (allow hal_usb_dp_relay_default vendor_usb_prop (file (read getattr map open)))
;;* lmx 19-33  untrusted_app_all / platform_app / system_app 均可 binder 调用并 find 该服务（对外是公开接口）
;;* lmx 41  (allow hal_usb_dp_relay_default hidg_device (chr_file (ioctl read write getattr open)))     ← /dev/hidg*
;;* lmx 44  (allow hal_usb_dp_relay_default input_device_202604 (dir (read open search)))
;;* lmx 45  (allow hal_usb_dp_relay_default input_device_202604 (chr_file (ioctl read write getattr open)))
;;* lmx 48  (allow hal_usb_dp_relay_default configfs_202604 (dir (read write create open add_name remove_name search rmdir)))
;;* lmx 49  (allow hal_usb_dp_relay_default configfs_202604 (file (read write create getattr setattr open)))
;;* lmx 50  (allow hal_usb_dp_relay_default configfs_202604 (lnk_file (read create getattr unlink)))   ← 建 function + symlink
;;* lmx 53/54  sysfs_202604 dir/file read
;;* lmx 55-57  sysfs_udc_202604 dir/file/lnk_file read                                                   ← 热插 UDC（cc500000.dwc3）
;;* lmx 60/61  vendor_sysfs_graphics  dir/file read
;;* lmx 64/65  vendor_sysfs_xm_power  dir/file read
;;* lmx 68  (allow hal_usb_dp_relay_default uhid_device_202604 (chr_file (ioctl read write open)))      ← /dev/uhid
;;* lmx 72  (allow hal_usb_dp_relay_default self (process (fork sigchld)))
```

同一个 `.te` 里引用的路径注释 —— **这就是 `adapter_svid` 的读取来源**：

```
-- /sys/class/typec/port0/svid
-- /sys/class/typec/port0/partner/svid
-- /sys/class/typec/port0/partner/*
```

串起来看：**读 typec partner 的 svid → 判 host type / `is_mac` → 用 configfs 建 `/config/usb_gadget/g1`
的 HID 函数与 symlink → 绑 UDC → 通过 `/dev/hidg*` 与主机交换 HID 报文；同时用 `/dev/uhid` 造虚拟输入
设备把坐标注回 Android**。

### 3.6 运行期事实

- 进程 pid `31218`，user = **root**
- AIDL 服务：id **399**，名 **`vendor.xiaomi.usb.dp.IUsbDpRelay/default`**
- binder 客户端：**`com.milink.service`**（pid 27544，tag `DPIN`）；HAL 打印 `tracking client pid 27544`
- `dumpsys vendor.xiaomi.usb.dp.IUsbDpRelay/default` → `FAILED_TRANSACTION`（该 HAL 未实现 dump）
- Mac 会话期间 `/dev/hidg0`、`/dev/hidg1` 存在；`/dev/uhid` = `crw-rw-rw- 1 uhid uhid 10,239`
- `getprop persist.vendor.usb.dp_relay` / `…_mode` 当前为**空**（属性在策略里，未被写入）

## 4. 证据三：主机侧最终看到什么

### 4.1 Mac（本仓库目标场景）

IOUSBHost 原始控制传输逐接口读 `GET_DESCRIPTOR(Report)`：

- 接口 `MI_02` = **107 字节**：`05 0d 09 02 …` → 只有 Pen（Usage Page `0x0D` / Usage `0x02`），
  **没有** `0x0D/0x04`（Touch Screen）、没有 `0x0D/0x22`（Finger）
- 接口 `MI_01` = **133 字节**：`05 01 09 06`（Keyboard）+ `05 01 09 02`（Mouse）
- 与第 2 节 `hid.usb0 desc: 133 bytes` / `hid.usb1 desc: 107 bytes` 完全对齐

平板侧同时刻的输入设备表里可见 `/dev/input/event18 name="dp_relay_touch"`
（即 HAL 用 `/dev/uhid` 造的虚拟设备，把外面来的坐标注回 Android），
以及 `Xiaomi Focus Pen Pro` / `… Keyboard` / `… Mouse`（event15–17）。

### 4.2 对照：同一块平板接 Windows

| 接口 | Windows 主机 | Mac 主机 |
|---|---|---|
| `MI_00` | WPD（MTP 语义） | MTP |
| `MI_01` | Keyboard + Mouse | 133 B（Keyboard + Mouse） |
| `MI_02` | **TouchScreen(`0x0D:0x04`) + Pen(`0x0D:0x02`)** | **仅 Pen（107 B）** |
| 手指触控 | **免驱即用**（用户实测） | **完全无反应** |

Windows 侧判据：早前在 Windows 上采集到 `HID_DEVICE_UP:000D_U:0004` 命中（原始采集文档
`xiaomi-pad-touch-diag.md` 未随仓库保存，见第 9 节）。两侧的 `VID/PID` 与 HID 描述符差异说明
**是同一套硬件按主机身份走了不同分支**，而不是线材/速率/协议栈问题。

## 5. 为什么主机侧软件修不了

`usb_dp_relay` 在 `setup (UDC:cc500000.dwc3)` 阶段（即 UDC 绑定、接口创建之前）就完成了
`is_mac` 判定，并按结果选择描述符。等 macOS 枚举到设备时，触摸屏接口**从来没有被创建**——
主机能看到的 HID 集合里，除笔以外的所有候选都为 0（全机 29 个 HID 设备中 `0x0D:0x04` 命中 0）。

主机侧所有能想到的手段（设备描述符指纹、MS OS 描述符探测、`SET_IDLE`、用户态补发控制请求、
IOHIDFamily 过滤绕过）均无法改变一个不存在的接口。可用的只剩「平板固件增加 Mac 分支的触摸输出」这一条。

设备侧同样动不了：`/sys/class/typec/`、`/config/usb_gadget/*`、`/odm/bin/*` 对 shell 全是
`Permission denied`，SELinux `permissive=0`，且本机无可用 root（GhostLock 路径在本内核已封死，
CVE-2026-43499 已修、官方提取器 exit 6）。**官方二进制的反汇编在本机做不到**，
能拿到的只有策略、配置、日志与主机侧描述符。

## 6. 建议修复（供小米评估，二选一即可）

1. **Mac 分支同样输出 Touch + Pen 描述符**（与 Windows 分支一致）。最小改动量：
   `if (is_mac) { use_pen_only_descriptor(); }` 里的分支收敛成同一套描述符。
   风险提示：Mac 会把多触点接口当成触摸屏设备，需要保证 Report 描述符符合 HID 触摸规范
   （本机型 Windows 分支已在用：`Report ID 1`，`10 × [Flags, ContactID, X16, Y16]` + `ContactCount`）。
2. **在系统设置里增加开关**：「DP-in 时向 Mac 输出手指触控」（默认关，用户自选）。
   好处是零风险、可回退。

## 7. 复现步骤（供工程师自证）

```bash
# 设备侧判定链
ADB=<platform-tools>/adb
$ADB shell 'ps -A -o PID,USER,ARGS | grep -i dp-service'          # pid 31218, user root
$ADB shell 'cat /odm/etc/init/vendor.xiaomi.usb.dp-service.rc'
$ADB shell 'cat /odm/etc/vintf/manifest/vendor.xiaomi.usb.dp-service.xml'
$ADB shell 'grep -n "usb_dp_relay" /vendor/etc/selinux/vendor_property_contexts'
$ADB shell 'grep -n "usb_dp_relay" /vendor/etc/selinux/vendor_file_contexts'
$ADB shell 'grep -n "hal_usb_dp_relay.te" /vendor/etc/selinux/vendor_sepolicy.cil'

# 进 DP-in（接 Mac）后读判定链
$ADB logcat -c
# 平板退出 DP-in → 拔线 → 重新 C2C 连 Mac → 进入 DP-in → 等笔可用 → 手指划几下
$ADB logcat -b all -d -v threadtime | grep -E 'usb_dp_relay|adapter_svid|host type|is_mac|hid\.usb'
```

预期即可看到第 2 节的 5 行。

**交叉验证（在 Windows 笔记本上做同一件事）**：把上面最后一条换成同样的 `grep` 命令，
预期出现 `host type: Windows` / `is_mac=0` / `hid.usb1 desc: 914 bytes`（触摸 + 笔）。
注意：ADB 在 DP-in 下会因 `disableNetworking` 断开，所以必须**先拔线、用 A→C/数据线重连 ADB 后读缓冲日志**。

## 8. 影响面

- 仅影响 **DP-in 外接 Mac** 这一种形态；平板自身触控、接 Windows/Linux 主机均正常。
- 与 macOS 版本、Mac 机型、线材无关（同一根官方线在 Windows 上手指可用，已实测双向反证）。
- 主机侧应用（本仓库的 Mipad DP Touch）只能承担「笔可用」这一半，无法承担「手指可用」。

## 9. 未证实 / 未采集（**推断，非证据**）

1. **Windows 分支的 `usb_dp_relay` 日志尚未采集** —— 现在只有两侧主机行为证据
   （Windows 插上就能摸；Mac 分支只建两路 HID 且无线触摸）。所以 Windows 侧
   `hid.usb1 desc: 914 bytes`（触摸 + 笔）是**预期值，不是实测值**；Windows 侧
   `HID_DEVICE_UP:000D_U:0004` 命中的原始采集文档已不在仓库中，需要时应在 Windows 上重采。
2. **`is_mac=1` 到底改了什么** —— 是少建一个 function，还是换了一份 report descriptor，
   需反汇编 `/odm/bin/vendor.xiaomi.usb.dp-service`。取二进制只有两条路：
   ① 下载官方 ROM 解包 `odm.img`；② root 后 `adb pull`。
3. `persist.vendor.usb.dp_relay(_mode)` 的取值语义未知（当前为空）。
4. 纯数据模式配置字节数差异（`0x2717` 39B vs `0x18d1` 44B）与 `persist.vendor.vcb.*` 仍未证实。
5. 平板自己的触摸面板在 DP-in 时是否仍在给 Android 上报，未测。

## 附：取证来源

- 设备直读（2026-09-18，adb 只读）：rc / vintf xml / property_contexts / file_contexts /
  `vendor_sepolicy.cil` / `ps` / `/dev` 与 sysfs 探测
- 既有抓包（`~/Desktop/`）：`mipad-mac-dpin-log.txt`（logcat，含第 2 节逐字行）、
  `mipad-mac-hid-dump.txt`（Mac 侧 HID 报告描述符）、`mipad-descriptor-now.txt`、
  `mipad-allinput.txt`（平板输入设备表）、`mipad-mac-lsusb.txt`、
  `mipad-mac-gadget-tree.txt`、`mipad-mac-diag-2026091*.zip`
- 仓库内相关：`README.md`（已知限制）、`INSTALL.md`（注意事项）、`CHANGELOG.md`
