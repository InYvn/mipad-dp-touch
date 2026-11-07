<div align="center">
  <h1>
    <img src="docs/logo.png?v=2.0.0-beta" width="170" alt="Mipad DP Touch Enhanced"/>
    <br/>
    Mipad DP Touch Enhanced
  </h1>
  <p>小米平板 9 Pro Max，变身 Mac 原生触控屏。</p>
  <p>通过 USB-C DP-in 连接，接入 macOS 27.3 系统原生触控。轻点、拖拽、滚动、右键与多指手势，直接在屏幕上完成。搭配小米焦点触控笔 Pro，笔尖上的点击、滚动与拖拽也一并就位。</p>
  <p><b>2.0.0-beta：接入 macOS 27.3 系统原生触控，更新五页原生设置界面。</b></p>
  <p><a href="https://inyvn.github.io/mipad-dp-touch/">官网与操作教程</a> · <a href="https://github.com/InYvn/mipad-dp-touch/releases">下载安装包</a> · <a href="CHANGELOG.md">更新日志</a></p>

![平台](https://img.shields.io/badge/platform-macOS%2027-blue)
![版本](https://img.shields.io/badge/version-2.0.0--beta-ff6900)
[![许可](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

## 2.0.0-beta 有什么变化

- 接入 macOS 27.3 系统原生触控，手指输入与手势交给系统处理，触控笔继续使用 App 的点击、滚动和拖拽映射。
- 默认按系统版本与触摸接口自动选择运行方式：macOS 27.3 及以上优先原生触控，较早系统使用兼容模拟，笔接口可独立使用。
- 原生触控下，已验证设备自动应用位置修正；「触控与位置」页显示实际修正状态，退出 App 后恢复。
- 设置窗口整理为五页，使用原生分段控件、轻量动作菜单和滑条。显示页按真实屏幕排列选择笔的目标屏，分辨率通过独立面板更改。

## 准备

- 小米平板 9 Pro Max、小米焦点触控笔 Pro，以及支持视频传输的 USB-C 全功能线。
- Mac 运行 macOS 27；使用系统原生触控时运行 macOS 27.3 或以上。
- 平板进入 DP-in，通过 USB-C 连接 Mac。
- 手指触控使用平板侧模块 `mipad-dp-touch-module-v1.0.zip`，安装时需要平板 root 权限。**已安装的旧模块可继续沿用，无需更新。**

## 按你的配置看功能

以下按 Mac 运行 **macOS 27**、App 使用默认「自动 · 推荐」列出；✅ 表示该配置可用，— 表示不提供该项功能。

| 用户配置 | DP-in 显示画面 | 笔操作 | 手指点击与拖拽 | 系统原生多点手势 | 自动触控位置修正 | App 显示设置 |
| --- | --- | --- | --- | --- | --- | --- |
| 无 App、无模块 | ✅ | 仅移动光标，无法点击 | — | — | — | — |
| 仅 App、无模块 | ✅ | ✅ | — | — | — | ✅ |
| App + 模块，macOS 27.2 及以下 | ✅ | ✅ | ✅ 模拟鼠标 | — | — | ✅ |
| App + 模块，macOS 27.3 及以上 | ✅ | ✅ | ✅ 系统原生 | ✅ | ✅ | ✅ |

- **DP-in 显示画面**由平板硬件与系统提供，不依赖 App 或模块；App 的显示设置提供分辨率、HiDPI 与刷新率档位选择，无需模块。
- **笔手势映射**通过 App 桥接笔的轻点、滚动、拖拽与右键，原厂笔接口无需模块即可使用。
- **兼容模拟**还提供手指双指滚动；**系统原生**模式下，手指输入与多点手势交给 macOS 处理。
- **macOS 27.3 及以上**：前往「系统设置 → 无障碍 → 触控 → 触控调节 → 设置」，完成引导并保持触控调节开启。旧平板模块可继续沿用。
- **自动触控位置修正**在 App + 模块的原生模式下，对已验证设备自动应用；退出 App 后恢复。

## 快速开始

### 1. 安装 Mac App

下载 [**2.0.0-beta 的 DMG**](https://github.com/InYvn/mipad-dp-touch/releases/download/v2.0.0-beta/Mipad-DP-Touch-2.0.0-beta.dmg)，打开后将 **Mipad DP Touch.app** 拖入「应用程序」，然后打开 App。

首次打开时，在「系统设置 → 隐私与安全性」中点「仍要打开」并确认。也可以在终端执行：

```bash
xattr -dr com.apple.quarantine "/Applications/Mipad DP Touch.app"
```

按 App 的分步引导完成**辅助功能**与**输入监控**授权，两项完成后点「立即重启」。

### 2. 准备平板触控模块

已有模块直接沿用。首次安装时，下载 [`mipad-dp-touch-module-v1.0.zip`](https://github.com/InYvn/mipad-dp-touch/releases/download/v2.0.0-beta/mipad-dp-touch-module-v1.0.zip)，在平板的 **KernelSU / Magisk 管理器 → 模块 → 从存储安装**中选择 ZIP，安装后重启平板，再重新连接 USB-C 或进入 DP-in。

临时 root 的脚本操作见[平板侧安装说明](tablet/README.md)。模块让平板在 DP-in 下提供触摸屏与笔接口。

### 3. 完成 macOS 原生触控设置

在 macOS 27.3 及以上，打开：

**系统设置 → 无障碍 → 触控 → 触控调节 → 设置**

完成系统引导，并保持**触控调节开启**。安装、打开 App 与完成这项系统设置的顺序不限。

App 的「触控与位置」保持「自动 · 推荐」即可。连接后，在「设备概览」查看当前运行方式，在「触控与位置」查看自动位置修正状态。

## 触控与笔操作

### 手指原生触控

macOS 27.3 原生模式下，手指输入由系统处理。「手势映射」页对应显示为「笔手势映射」，可继续单独调整笔的动作、滚动方向和速度。

「触控与位置」提供「自动 · 推荐」「原生触控」「兼容模拟」三种选择。默认使用自动选择。

### 触控笔手势

| 手势 | 默认动作 |
| --- | --- |
| 轻点 | 鼠标左键单击 |
| 快速滑动 | 滚动翻页 |
| 停住再滑 | 拖拽窗口、图标或滑块 |
| 停住不动后抬手 | 右键菜单 |

在「手势映射」中点当前动作，通过分组菜单更换绑定。快速滑动可改为「拖拽 · 选中文字」；多屏使用时，可将手势绑定为「切换屏幕」，依次切换笔的光标基准屏。点「恢复默认」恢复默认绑定。

滚动方向使用分段控件切换，速度通过滑条选择并显示具体倍率，也可恢复默认速度。高级选项可调整「停住再滑」与「停住不动」的判定时长。

## 五页原生设置界面

点击菜单栏图标 →「设置…」，或按 **⌘,** 打开设置窗口。侧栏使用系统玻璃材质，可通过 **⌘1–5** 切页。

![设备概览](docs/assets/settings-overview.png?v=2.0.0-beta)

**2.0.0-beta 界面预览**

<details>
<summary>查看其余四页</summary>

### 触控与位置

![触控与位置](docs/assets/settings-touch.png?v=2.0.0-beta)

### 笔手势映射

![笔手势映射](docs/assets/settings-gestures.png?v=2.0.0-beta)

### 显示设置

![显示设置](docs/assets/settings-display.png?v=2.0.0-beta)

### 运行与诊断

![运行与诊断](docs/assets/settings-runtime.png?v=2.0.0-beta)

</details>

| 页面 | 内容 |
| --- | --- |
| 设备概览 | 连接状态、权限引导、当前运行方式及自动或手动来源 |
| 触控与位置 | 触控方式、自动位置修正状态、系统触控设置入口 |
| 手势映射 | 笔手势动作菜单、滚动方向与速度、增强模式 |
| 显示设置 | 当前分辨率、HiDPI 与刷新率摘要、分辨率面板、真实屏幕排列与笔目标屏选择 |
| 运行与诊断 | 笔桥接开关、开机自启、日志、设备诊断与高级选项 |

「显示设置」中，点击屏幕排列图上的在线屏幕即可选择笔的光标基准屏，也可选「自动」或使用列表选择。分辨率点「更改…」，在面板中选择档位后点「应用」。

菜单栏提供桥接开关、显示缩放、光标基准屏、设置、日志和更新入口。菜单与设置窗口共用配置，设置保存在 `~/Library/Application Support/MipadDPTouch/config.json`。

## 更新与卸载

菜单栏「检查更新…」和高级选项中的「立即检查更新」可检查新版本。有更新时点「下载并安装」，App 会下载、校验 SHA256、替换并重新打开。「高级选项 → 自动检查更新」控制自动检查。

卸载 Mac App 时，退出 App，再从「应用程序」删除。平板模块通过 KernelSU / Magisk 管理器卸载；脚本方式见[平板侧说明](tablet/README.md#卸载)。

## 从源码构建

```bash
git clone https://github.com/InYvn/mipad-dp-touch.git
cd mipad-dp-touch
./build_app.sh      # 需要 uv，脚本准备 Python 环境并生成 dist-pyi/Mipad DP Touch.app
./make_dmg.sh       # 生成 DMG 安装包
```

## 文档

- [INSTALL.md](INSTALL.md)：安装、权限授权与卸载说明。
- [平板侧说明](tablet/README.md)：模块安装与临时 root 脚本操作。
- [CHANGELOG.md](CHANGELOG.md)：版本更新记录。
- [官网](https://inyvn.github.io/mipad-dp-touch/)：操作教程。

## 许可

本项目使用 **MIT License**，见 [LICENSE](LICENSE)。
