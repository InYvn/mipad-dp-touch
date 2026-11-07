# 平板侧：让手指触控也能传给 Mac

Mac 端 App 按系统版本选择原生触控或模拟鼠标；这个目录是**平板侧**的配套 ——
平板在 DP-in 认出 Mac 之后，默认只把「笔」那一路建给主机（`hid.usb1` 写成 107 字节的纯笔描述符），
手指触摸的数据根本不发出来。这里用一个**内存态的 bind-mount** 把官方固件的判定骗过去，
让它按「非 Mac 主机」建出 914 字节的「触摸屏 + 笔」二合一接口。

> **运行时覆盖**：模块文件放在 `/data/adb/mipad_dptouch/`，bind-mount 盖在 sysfs 节点上；
> 不刷系统分区、不改 `/odm`；通过运行时 bind-mount 暴露触摸接口。
> 反汇编与实测全过程见 [`../docs/firmware-issue-apple-svid.md`](../docs/firmware-issue-apple-svid.md)。

## 安装模块

已安装的旧 v1.0 模块可继续沿用，无需更新。首次安装需要平板 root 权限：

1. 下载 [`mipad-dp-touch-module-v1.0.zip`](https://github.com/InYvn/mipad-dp-touch/releases/download/v2.0.0-beta/mipad-dp-touch-module-v1.0.zip)，保存到平板。
2. 打开平板的 **KernelSU / Magisk 管理器 → 模块 → 从存储安装**，选择这个 ZIP。
3. 安装后使用热重启模块完成热重启。
4. 重新连接 USB-C 或进入 DP-in，Mac 侧即可检测到触摸屏接口。

模块卡片上的「**运行**」按钮 = 一键停用 / 恢复：

* 点一下 → 停用（触摸屏接口不再建给主机，笔照常）
* 再点一下 → 恢复

日志在 `/data/adb/mipad_dptouch/log.txt`。

## 怎么验证成功了

热重启并重新连接后，打开 Mac App 的设置窗口，在「触控与位置」查看触摸接口与当前生效状态，在「设备概览」查看当前运行方式。

## 卸载

在平板的 KernelSU / Magisk 管理器中卸载模块，再重新连接 USB-C 或进入 DP-in。

## 适用范围

* 只在 **小米平板 9 Pro Max**（`xring_o3_asic` / OS4.0.x）上实测过。
  模块会检查目标 sysfs 节点是否存在。
* 官方一旦修掉这个判定（把触摸那一路也建给 Mac），本模块就没用了 —— 直接卸载。
* 模块里**不含任何小米的二进制**，只有几个 shell 脚本。
