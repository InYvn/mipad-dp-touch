# Mipad DP Touch Fullsplit — 2.1.10

供管理器重装验收的完整本地候选，不是公开发布版本（未 push、未公开发布）。2.1.10 已由 Lead 完成一次真机端到端验收（接管/原生输入/Android 输入/退出释放，证据见 `.scratch/mipad-native-touch/evidence/FINAL-E2E-210.md` 及其 T8 审查修正节），但**只有这一次生产包会话**，且原始 logcat/hidutil/configfs 读回等证据形态有限；管理器真实重装和软重启后自动启动仍待验收，不能据此推断每项手势/每次连接均通过。

## 安装与重新安装

仅限 KernelSU、Enforcing、arm64、yingtian / M367FC / O3 / xring_o3_asic，以及 manifest 中精确 HAL/init 契约。Magisk、Recovery、其他硬件/未知固件不支持。临时 root 的热重启保留能力也须实机验证，不能以正常重启替代。

1. 打开 KernelSU 管理器 → 模块 → 从存储安装，选择 `mipad-dp-touch-module-v2.1.10-os4.0.16.zip`（本次为本地构建产物，未发布到远端）。
2. **跨 generation 升级（2.1.9 及更早 → 2.1.10）**：安装前先移走旧运行时，并在不重启的热切换场景清掉旧 `/dev` stage（重启会清 `/dev`，正常安装后重启不受影响）：
   `su -c 'mv /data/adb/mipad_fullsplit_local /data/adb/mipad_fullsplit_local.bak-<时间戳>'`
   `su -c 'mv /dev/mipad-fullsplit-v104-local /dev/mipad-fullsplit-v104-local.bak-<时间戳>'`
   否则安装器会按 generation guard 拒绝，或 `stage()` 因旧 DSO pin 不匹配而反复失败（约 30s 后 runtime-monitor 写 `disable`，可用包内 `action.sh` 清标志后重试）。
3. 当前同 ID 且运行时字节完全一致时，安装器允许覆盖重装，只更新管理器模块文件；不覆盖持久运行时、不停止 HAL、不回收任何 owner。若提示不同 runtime/旧 SVID 模块/外来状态，停止操作，保留现状，不要盲卸或正常重启绕过保护。
4. 安装成功后确认模块已启用。正常开机/管理器热重启时 KernelSU 会自动执行包内 `service.sh`，**不需要任何外部启动脚本**；若软重启后 monitor 未常驻（已知 KernelSU LKM 晚加载限制），以 root 直接执行**包内**服务脚本即可：
   `su -c 'sh /data/adb/modules/mipad_dptouch_fullsplit_local/service.sh'`
   然后重新连接 USB-C 或进入 DP-in。
5. 首次验收核对版本及真实 DSO、native62、持久 owner、Mac B 状态，并重新实测手指和笔。真实管理器重装及软重启自动启动完成前，仍只称候选。

旧模块或不同运行时的跨代迁移不在此次“相同字节安全重装”支持范围，保护不会被跳过；需要单独安排受控恢复和所有权交接，不能以卸载代替。

## 自动生命周期

- `service.sh` 验证 KernelSU 域、硬件/HAL/init、Enforcing、init mount namespace；`post-fs-data.sh` 不变更设备。
- 原生 controller 的 flock 序列化 staging、guardian、worker；常驻 monitor 自动重建 supervisor。没有固定 HAL PID、临时 owner 路径、外部试验目录或试验截止时间依赖。
- 完整包 `files.sha256`、管理器保留输入 `installed.sha256`、持久运行时 `runtime.sha256` 分别验完整性；管理器可删除 customize/README 而不破坏启动。
- 自有 `/dev` tmpfs backing 使用精确已验证标签；write-ahead journal 留在 `/data/adb/mipad_fullsplit_local`。不改原 sysfs 标签、不放宽 SELinux。
- 按真实原厂 Binder 生命周期 `release → stop → quiesce → candidate → persistent prepare` 重建 gadget：stop 后先有界等待 init 确认停止与旧 PID 消失（hidg 节点消失只做 grace 观察，不阻断），再经静默窗口后挂 SVID 并 launch；HAL 被重新拉起即中止接管并恢复 stock。恢复时必要的 `prepare → release → prepare` 由持续客户端完成，不写 UDC/configfs。
- recover 安全边界（2.1.5，依据 2026-11-13 00:54 真机证据）：跨 boot 的 stale pending/journal 直接清理且不触碰 HAL/SVID/owner；已认证的健康 candidate 是 no-op（不 release/terminate/rebuild）；owner-rebuild 对健康会话跳过并按 30s 窗口节流，避免每 tick 触发 OEM HAL releaseGadget 拆 gadget。
- 退出释放（2.1.6，依据 2026-11-13 01:40 真机证据）：以 UDC 状态为 host 数据链路权威证据——任一可读 UDC `configured` → present，全部可读 UDC `not attached` → absent（candidate 自建的 configfs HID 不再作为会话证据）；absent 持续 15s 后自动释放 candidate，恢复平板触控；UDC 不可读/混合时回退 DRM/typec/HID 的 present 证据，unknown 不释放。
- DP-in 显示模式门禁与退出释放（2.1.7，依据 2026-11-13 01:46–01:53 真机证据）：stock DP HAL 出现 ≠ 用户进入 DP-in 显示模式（当时 top activity 仍是 miui Home 却已接管）。新增 dpin_display_mode()（有界 `timeout 3 dumpsys activity activities`，topResumedActivity 为 `com.milink.dpin.ui.display.DisplayModeActivity` → active；其他 activity → inactive；失败/超时/空/无 top 行 → unknown）：reconcile 在任何 mutation 前要求 active，否则 `DPIN_MODE_INACTIVE state=…; wait` 并 rc=78 等待；watchdog 在 candidate 仍认证时采样，持续 inactive ≥15s → `RELEASED reason=dpin_mode_lost`；unknown 不接管也不释放。
- 释放收尾（2.1.8，依据 2026-11-13 真机证据）：watchdog 释放 candidate 后，先按身份终止 journaled prepare-owner（无 orphan）、再经 platform.sh `release-clean` 卸载 fake SVID bind 并验证 mount 消失/owner 记录清除（读回原厂 05ac），全部成功后才拉起 stock HAL；任一失败 fail-closed：保留 pending 给 recover() 重试、延迟 stock HAL 恢复（`STOCK_HAL_DEFERRED`）。
- 原生触控 + 双端同触（2.1.10，依据 G1/G2 真机证据）：固定 V4 描述符替换（Touch Pad 0x0D:0x05 + RID2→RID1）与默认 no-grab（event6/9/11/7/8），见上节。
- 锁屏/息屏释放（2.1.9，依据 2026-11-13 真机证据）：退出 DP-in 后若平板锁屏/息屏，`dumpsys activity activities` 没有 topResumedActivity（旧判据 unknown → candidate 永久 grab、用户无法触摸解锁）。新增：top 缺失/null 时再查 `dumpsys display` 的 mState=OFF 或 `dumpsys window` 的 mDreamingLockscreen/keyguard → `screen_off|locked`（仅释放证据，不参与接管门禁）；watchdog 持续 inactive 或 screen_off/locked ≥15s → `RELEASED reason=dpin_mode_lost|session_ended_screen_off`；unknown 仍不接管不释放。
- 核对真实 HAL 的 PID/starttime/exe、域、uid/gid/groups、AT_SECURE=0、精确可执行 DSO mapping、双 hidg、活 pidfd owner 和 descriptor。新 owner 真正接管后才退休旧自有 owner；已删除 exe 的完整身份字符串仍严格保留。
- 崩溃、disable/remove 走同一恢复事务。禁用/卸载后不再启动 sender；原厂 HAL 仍依赖自有 prepare client 时保留最小恢复监控，直到系统 owner 接管。不要删除活动 owner 所依赖的持久目录。

## 原生触控 + 双端同触（2.1.10，G1/G2 真机验证后集成）

2.1.10 在 2.1.9 的 fullsplit 门禁之上固定两项行为（无实验开关、无环境变量选择）：

- **V4 HID 描述符**：平板侧把 `hid.usb1` 的报告描述符替换为经真机验证的 V4 形态——应用集合 usage 从 Touch Screen(0x0D:0x04) 改为 **Touch Pad(0x0D:0x05)**，feature report ID 2 改为 report ID 1；长度仍 914B、report_length 仍 62B、62B 帧布局不变（不做帧重排）。macOS 只在 Touch Pad 形态下把 digitizer 帧派发成系统原生输入（点击/拖拽/双指滚动/轻扫），V0/V1/V2/V3/V6 与 V5 均未通过 G1。
- **默认 no-grab**：stock HAL 会对 5 个 evdev 节点调用 EVIOCGRAB（`event6` 是 Android 主触摸屏），2.1.10 的 DSO 把这 5 个节点（`event6,event9,event11,event7,event8`）的 EVIOCGRAB/EVIOCREVOKE 吞掉（返回成功、不下发），使 Android 与 macOS 同时收到触摸（G2 真机验证：Mac 原生事件 + Android getevent 同时收到，双端操作正常）。

配套要求与诊断：

- Mac App 需要 **2.2.3-beta 或更新**：`hid_bridge.match_usages` 在 auto/touch 模式下接受 0x0D:0x05（Touch Pad），App 日志出现「触控接口已识别」；位置校正仍依赖 EDID 校正（applied 282×188）。
- **诊断逃生**：仅用于排查。在平板上创建 `/data/adb/mipad_fullsplit_local/allow-grab`（root 属主普通文件）后重启 monitor/重新进入 DP-in，DSO 恢复 stock HAL 的内核 grab（Android 触控会被独占，macOS 原生仍可用，取决于 HAL 状态）；删除该文件并重启 monitor 恢复默认双端。
- **描述符替换是 fail-closed**：只有 stock HAL 单次写入的缓冲恰好是整份 914B factory 描述符时才替换；写入失败/内容不符时 DSO 保留 factory HID 形态并只记录一次 `native-desc-*` 日志，模块的候选描述符契约会中止接管（原生不生效），App 的 legacy（兼容模拟）路径仍可使用 2.1.9 行为。此时请回滚 2.1.9 或检查 HAL 版本。
- **EDID 与退出状态**：原生位置校正依赖 EDID 校正 applied（282×188 mm，读回 280.1×189.8 mm）。退出 DP-in 且 DP 显示已被系统移除时，EDID 助手无法回读目标屏，恢复尝试可能以 `failed`（`reset API/readback verification failed` / `guard still alive`）结束——这是**未确认状态**，不是平板/内屏 EDID 被改坏，也不影响平板自身使用；下次进入 DP-in 会重新 apply。「位置准确」目前仅有用户主观确认，尚无设备端坐标对照测量。
- **no-grab 的已知 fail-open 分支**：若 `/proc/self/fd` readlink 失败（极端 fd/权限竞态）或 HAL 抓取白名单之外的节点，DSO 会把 grab 透传给内核（fail-open），此时 Android 触控可能被 HAL 独占；白名单外节点的转发会记 `native-grab-unmatched … action=forward`。诊断优先用 `$S/allow-grab` 对照（恢复内核 grab 后日志 `native-grab-allowed`），并用 `adb shell getevent /dev/input/event6` 判断 Android 是否收到事件；需要重新 probe HAL 的 grab 列表/权限后更新白名单再发版。
- 关键日志（tag `FullSplitBounded`，新增 native 日志只在启动/接管时各一条，不逐帧打印；2.1.9 既有的 gate 帧日志 `input_seq/seq hex=…` 保持不变、仍逐帧）：`native init variant=V4 … no_grab=1 allow_grab=0`、`native-desc-replaced variant=V4 …`、`native-desc-replace-failed`/`native-desc-write-unexpected`、`native-grab-suppressed path=/dev/input/event6 …`（每设备一次）及其余 `native-grab-unmatched`。

## 发送端与范围

fullsplit bounded-v2 处理库（2.1.10：2.1.9 门禁行为 + 固定 V4 描述符替换 + 默认 no-grab；仍保留 2.1.4 门禁修复——瞬时写失败不再永久毒化，release 帧整包成功才自愈）：

`73234ec8bd1f06b744110f227b22514be918e6b05d0dc3a7555e644760a197f5`

保持 explicit mode1、FIFO、有界重试、FD 世代、短写隔离、report3 透传。无 HDR、个人精调或厂商二进制。模块只提供平板发送端，Mac 的生产 App/B 与系统设置仍由原链路管理，不由此 ZIP 安装或修改。

宿主状态机、Android 自有进程及真实 KernelSU BusyBox 沙箱验证不等于真实管理器安装、热重启或长期拔插验收。整体“正常”不自动转换为点击、拖动、长按分别通过。
