# Mipad DP Touch Fullsplit — 2.1.3

供管理器重装验收的完整本地候选，不是公开发布版本。相同运行时代码已由生产 service/controller 在已验证设备启动，读回 native62、处理库与持久 owner，用户整体反馈正常；管理器真实重装和热重启自动启动仍待验收，不能据此推断每项手势均通过。

## 安装与重新安装

仅限 KernelSU、Enforcing、arm64、yingtian / M367FC / O3 / xring_o3_asic，以及 manifest 中精确 HAL/init 契约。Magisk、Recovery、其他硬件/未知固件不支持。临时 root 的热重启保留能力也须实机验证，不能以正常重启替代。

1. 打开 KernelSU 管理器 → 模块 → 从存储安装，选择 Download 中的 `mipad-dp-touch-module-v2.1.3-os4.0.16.zip`。
2. 当前同 ID 且运行时字节完全一致时，安装器允许覆盖重装，只更新管理器模块文件；不覆盖持久运行时、不停止 HAL、不回收任何 owner。若提示不同 runtime/旧 SVID 模块/外来状态，停止操作，保留现状，不要盲卸或正常重启绕过保护。
3. 安装成功后确认模块已启用，使用已安装的热重启模块完成热重启，再重新连接 USB-C 或进入 DP-in。无需额外脚本、手动复制处理库或执行命令。
4. 首次验收核对版本及真实 DSO、native62、持久 owner、Mac B 状态，并重新实测手指和笔。真实管理器重装及热重启完成前，仍只称候选。

旧模块或不同运行时的跨代迁移不在此次“相同字节安全重装”支持范围，保护不会被跳过；需要单独安排受控恢复和所有权交接，不能以卸载代替。

## 自动生命周期

- `service.sh` 验证 KernelSU 域、硬件/HAL/init、Enforcing、init mount namespace；`post-fs-data.sh` 不变更设备。
- 原生 controller 的 flock 序列化 staging、guardian、worker；常驻 monitor 自动重建 supervisor。没有固定 HAL PID、临时 owner 路径、外部试验目录或试验截止时间依赖。
- 完整包 `files.sha256`、管理器保留输入 `installed.sha256`、持久运行时 `runtime.sha256` 分别验完整性；管理器可删除 customize/README 而不破坏启动。
- 自有 `/dev` tmpfs backing 使用精确已验证标签；write-ahead journal 留在 `/data/adb/mipad_fullsplit_local`。不改原 sysfs 标签、不放宽 SELinux。
- 按真实原厂 Binder 生命周期 `release → stop → candidate → persistent prepare` 重建 gadget；恢复时必要的 `prepare → release → prepare` 由持续客户端完成，不写 UDC/configfs。
- 核对真实 HAL 的 PID/starttime/exe、域、uid/gid/groups、AT_SECURE=0、精确可执行 DSO mapping、双 hidg、活 pidfd owner 和 descriptor。新 owner 真正接管后才退休旧自有 owner；已删除 exe 的完整身份字符串仍严格保留。
- 崩溃、disable/remove 走同一恢复事务。禁用/卸载后不再启动 sender；原厂 HAL 仍依赖自有 prepare client 时保留最小恢复监控，直到系统 owner 接管。不要删除活动 owner 所依赖的持久目录。

## 发送端与范围

fullsplit bounded-v2 处理库（2.1.3 门禁修复：瞬时写失败不再永久毒化，release 帧整包成功才自愈）：

`72757954666bcca58a022c546f81fefb1bb7ffef4a3250fcc9236982bc79c488`

保持 explicit mode1、FIFO、有界重试、FD 世代、短写隔离、report3 透传。无 HDR、个人精调或厂商二进制。模块只提供平板发送端，Mac 的生产 App/B 与系统设置仍由原链路管理，不由此 ZIP 安装或修改。

宿主状态机、Android 自有进程及真实 KernelSU BusyBox 沙箱验证不等于真实管理器安装、热重启或长期拔插验收。整体“正常”不自动转换为点击、拖动、长按分别通过。
