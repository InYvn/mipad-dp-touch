# 平板侧：让手指触控也能传给 Mac

Mac 端 App（**Mipad DP Touch 1.2+**）负责把触控翻译成鼠标；这个目录是**平板侧**的配套 ——
平板在 DP-in 认出 Mac 之后，默认只把「笔」那一路建给主机（`hid.usb1` 写成 107 字节的纯笔描述符），
手指触摸的数据根本不发出来。这里用一个**内存态的 bind-mount** 把官方固件的判定骗过去，
让它按「非 Mac 主机」建出 914 字节的「触摸屏 + 笔」二合一接口。

> **只动内存**：假文件放在 `/data/adb/mipad_dptouch/`，bind-mount 盖在 sysfs 节点上；
> 不刷分区、不改 `/odm`、不 remount、不碰任何系统文件。卸载或重启即完全复原。
> 反汇编与实测全过程见 [`../docs/firmware-issue-apple-svid.md`](../docs/firmware-issue-apple-svid.md)。

## 先看你是哪种 root

| 你的情况 | 用哪个 | 说明 |
| --- | --- | --- |
| **永久 root**（解锁 BL + KernelSU/Magisk） | **装模块**（`module/`，见下） | 每次开机自动应用，不用管 |
| **临时 root**（提权工具，重启就没了） | **跑脚本** `mac-touch.sh on` | 每次提权后跑一次 |

两种方式共用同一份状态目录 `/data/adb/mipad_dptouch/`，不会互相打架。

## 装模块（永久 root）

从 [Releases](https://github.com/InYvn/mipad-dp-touch/releases) 下载 `mipad-dp-touch-module-v1.0.zip`
（和 Mac 端 App 放在同一个 release 里），传到平板：

```bash
adb push mipad-dp-touch-module-v1.0.zip /sdcard/Download/
```

<details><summary>从源码自己打（开发者）</summary>

```bash
./build_module.sh                                  # 产出 dist/mipad-dp-touch-module-v1.0.zip
adb push dist/mipad-dp-touch-module-v1.0.zip /sdcard/Download/
```

</details>

然后在平板上：**KernelSU / Magisk 管理器 → 模块 → 从存储安装 → 选这个 zip → 重启**。

重启后拔插一次 USB-C（或退出 DP-in 再进），Mac 侧就能看到触摸屏接口了。

模块卡片上的「**运行**」按钮 = 一键停用 / 恢复：

* 点一下 → 停用（触摸屏接口不再建给主机，笔照常）
* 再点一下 → 恢复

日志在 `/data/adb/mipad_dptouch/log.txt`。

## 跑脚本（临时 root）

```bash
adb push mac-touch.sh /data/local/tmp/
adb shell su -c 'sh /data/local/tmp/mac-touch.sh on'      # 开启
adb shell su -c 'sh /data/local/tmp/mac-touch.sh status'  # 看状态
adb shell su -c 'sh /data/local/tmp/mac-touch.sh off'     # 还原
```

`on` 之后拔插一次 USB-C（或重进一次 DP-in）生效。**重启后失效**，重新 root 再跑一次即可。

## 怎么验证成功了

平板侧（`on` 之后拔插一次 USB-C）：

```bash
adb shell 'logcat -d | grep usb_dp_relay | tail -5'
```

应该看到这三行（关键是最后一行从 `107 bytes` 变成 `914 bytes`）：

```
usb_dp_relay: adapter_svid: read failed        # 读到的是假值
usb_dp_relay: host type: Windows               # 判定成"非 Mac 主机"
usb_dp_relay: hid.usb1 desc: 914 bytes         # 建出触摸屏+笔二合一接口
```

Mac 侧：打开 App 的设置窗口 →「笔的输入」→「增强模式」右边应显示
**「已检测到触摸屏接口 · 手指可直接点击 / 拖拽」**。

## 卸载

* 模块：管理器里卸载（会顺手摘掉挂载），或 `uninstall.sh` 里的动作
* 脚本：`mac-touch.sh off`
* 彻底清理：`rm -rf /data/adb/mipad_dptouch`

## 适用范围

* 只在 **小米平板 9 Pro Max**（`xring_o3_asic` / OS4.0.x）上实测过。
  脚本会先检查那个 sysfs 节点在不在，不是这台机器就**什么都不做**。
* 官方一旦修掉这个判定（把触摸那一路也建给 Mac），本模块就没用了 —— 直接卸载。
* 模块里**不含任何小米的二进制**，只有几个 shell 脚本。
