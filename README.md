<div align="center">
  <h1>
    <img src="docs/logo.png?v=2.0.0-beta" width="170" alt="Mipad DP Touch Enhanced"/>
    <br/>
    Mipad DP Touch <img alt="Enhanced" src="https://img.shields.io/badge/-Enhanced-ff6900?style=flat-square">
  </h1>
  <p>小米平板 9 Pro Max，变身 Mac 原生触控屏。</p>
  <p>通过 USB-C DP-in 连接，把小米平板变成 Mac 的触控屏：手指的轻点、拖拽、滚动与双指手势由 App 的兼容模拟完成（macOS 27.3 的系统原生触控对模块 HID 实测不出事件，默认不启用）；笔尖上的点击、滚动与拖拽也一并就位。</p>
  <p><a href="https://inyvn.github.io/mipad-dp-touch/">官网与操作教程</a> · <a href="https://github.com/InYvn/mipad-dp-touch/releases">下载安装包</a> · <a href="CHANGELOG.md">更新日志</a></p>

![平台](https://img.shields.io/badge/platform-macOS%2027-blue)
![版本](https://img.shields.io/badge/version-2.0.0--beta-ff6900)
[![许可](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

## 准备

- 小米平板 9 Pro Max、小米焦点触控笔 Pro，以及支持视频传输的 USB-C 全功能线。
- Mac 运行 macOS 27。兼容模拟（默认）在 macOS 27 各版本都可用；系统原生触控只存在于 macOS 27.3 及以上，且本版本实测对模块 HID 不出事件，默认不启用（见「手指触控」一节）。
- 平板进入 DP-in，通过 USB-C 连接 Mac。
- 手指触控使用平板侧模块 `mipad-dp-touch-module-v2.0.0.zip`，提供触摸屏与笔接口，并优化双指缩放；安装需要平板 root 权限和匹配的硬件及固件。

## 按你的配置看功能

以下按 Mac 运行 **macOS 27**、App 使用默认「自动 · 推荐」列出；✅ 表示该配置可用，— 表示不提供该项功能。「自动」在 macOS 27.3 及以上默认走兼容模拟（原生能力未验证）。

| 用户配置 | DP-in 显示画面 | 笔操作 | 手指点击与拖拽 | 系统原生多点手势 | 自动触控位置修正 | App 显示设置 |
| --- | --- | --- | --- | --- | --- | --- |
| 无 App、无模块 | ✅ | 仅移动光标，无法点击 | — | — | — | — |
| 仅 App、无模块 | ✅ | ✅ | — | — | — | ✅ |
| App + 模块，macOS 27.2 及以下 | ✅ | ✅ | ✅ 兼容模拟（默认） | — | — | ✅ |
| App + 模块，macOS 27.3 及以上 | ✅ | ✅ | ✅ 兼容模拟（默认） | — 实测不可用 | —（仅原生模式） | ✅ |

- **DP-in 显示画面**由平板硬件与系统提供，不依赖 App 或模块；App 的显示设置提供分辨率、HiDPI 与刷新率档位选择，无需模块。
- **笔手势映射**通过 App 桥接笔的轻点、滚动、拖拽与右键，原厂笔接口无需模块即可使用。
- **兼容模拟是 macOS 27.3+ 的默认**：手指输入由 App 合成鼠标事件（含双指滚动/缩放），需要先在系统设置关闭触控手势。「原生触控」只在确认本机原生可用时手选；本版本实测 macOS 27.3 的系统原生触控对模块 HID 不出事件。
- **自动触控位置修正**只在原生模式生效；兼容模拟不依赖它（手指坐标由 App 侧映射）。

## 快速开始

### 1. 安装 Mac App

从 [Releases](https://github.com/InYvn/mipad-dp-touch/releases) 选择 **2.0.0-beta** 的 DMG，打开后将 **Mipad DP Touch.app** 拖入「应用程序」，然后打开 App。

首次打开被系统拦下时（ad-hoc 签名），在「系统设置 → 隐私与安全性 → 安全性」中点「仍要打开」并确认。

按 App 的分步引导完成**辅助功能**与**输入监控**授权，两项完成后点「立即重启」。

### 2. 准备平板触控模块

下载 [`mipad-dp-touch-module-v2.0.0.zip`](https://github.com/InYvn/mipad-dp-touch/releases/download/v2.0.0-beta/mipad-dp-touch-module-v2.0.0.zip)，保存到平板，在 平板的模块管理器 → 模块 → 从存储安装中选择 ZIP → 确认启用 → 使用热重启模块完成热重启 → 重新连接 USB-C 或进入 DP-in。模块提供触摸屏与笔接口，并优化双指缩放。

安装前提：KernelSU 管理器、root、Enforcing、arm64、Android 15（API 35）或以上，以及匹配的小米平板 9 Pro Max（yingtian / M367FC / O3 / xring_o3_asic）硬件与 HAL/init 固件契约。

已安装 v1.0，请先在管理器卸载旧模块并热重启，再安装 2.0.0。若安装器仍提示旧模块或不同运行时状态，请停止安装；该状态不能直接覆盖。同 ID 且运行时字节一致的 2.0.0 可覆盖重装。

### 3. 触控方式（默认兼容模拟，无需系统设置）

**默认「自动 · 推荐」在 macOS 27.3 及以上使用兼容模拟**，不需要开启系统触控手势。如果之前按旧教程开启过「触控调节」，请在 **系统设置 → 无障碍 → 触控 → 触控调节** 关闭它，避免系统与桥接双重派发。

只有想验证/使用系统原生触控时，才去开启这项设置：**系统设置 → 无障碍 → 触控 → 触控调节 → 设置**，完成系统引导并保持开启，然后在 App 的「触控与位置」手选「原生触控」。注意本版本在 macOS 27.3 实测原生触控对模块 HID 不出事件（见下文），手选后手指可能完全没反应。

连接后，在「设备概览」查看当前运行方式，在「触控与位置」查看实际选路与原因。

## 触控与笔操作

### 手指触控：默认兼容模拟

「触控与位置」提供「自动 · 推荐」「原生触控」「兼容模拟」三种选择，笔接口独立可用：

- **自动 · 推荐（默认）**：macOS 27.3 及以上使用**兼容模拟**，较早系统也是兼容模拟。原因：本版本实测 macOS 27.3 的系统原生触控对模块 HID 不产生事件（内核日志 `createEventForDigitizerCollection generates null event`），只有 App 的兼容模拟能派发手指。自动模式只有在确认过原生能力（配置 `native_confirmed: true`，或手选过一次「原生触控」）时才会选原生。
- **原生触控**：手指输入与多指手势交给 macOS。需要先在系统设置开启触控调节，否则手指没有反应。**本版本在 macOS 27.3 实测不可用**：手选后手指可能完全没反应，改选「兼容模拟」即可恢复。手选「原生触控」会记录「原生能力已确认」，之后切回「自动」仍会用原生；改选「兼容模拟」（确认关闭系统手势）会撤掉该记录，自动模式回到兼容模拟。
- **兼容模拟**：App 合成手指的点击、拖拽、滚动与双指手势，需要先关闭系统触控手势；「手势映射」页显示为「手势映射」，可同时调整手指与笔。

> 想彻底让「自动」回到兼容模拟，也可以退出 App 后把 `~/Library/Application Support/MipadDPTouch/config.json` 里的 `native_confirmed` 改成 `false`（App 运行中直接改也会被采纳，无需重启）。

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

**界面预览**

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

卸载 Mac App 时，退出 App，再从「应用程序」删除。平板模块通过平板的模块管理器卸载，详见[平板侧说明](tablet/README.md#卸载)。

## 可选插件：屏幕键盘（TouchKeys）

屏幕键盘**不随 App 打包**。菜单 →「插件 → 屏幕键盘（TouchKeys）…」：

1. 首次点开时先查公开仓库（`InYvn/TouchKeys`）的最新插件发版，弹窗写明来源、版本、大小；
2. 你点「下载并安装」后，App 才现场下载 `touchkeys-plugin-*.zip`，按发版正文里的 SHA256 校验，解压到 `~/Library/Application Support/MipadDPTouch/plugins/touchkeys/<version>/`；
3. 插件在 dptouch 进程内运行，**共用** App 的「设备控制和数据访问」权限，不会多出 Dock/菜单栏图标；
4. 「跟随输入框自动呼出」可在插件子菜单里开关；卸载走「插件 → 卸载屏幕键盘插件…」。

校验与兼容：发版正文没有该资产的 SHA256 就拒绝安装；`plugin.json` 里的 `id` / `api` / `min_host` 不匹配也拒绝。插件加载失败时菜单会给出错误，不会静默。

## 从源码构建

```bash
git clone https://github.com/InYvn/mipad-dp-touch.git
cd mipad-dp-touch
./build_app.sh      # 需要 uv，脚本准备 Python 环境并生成 dist-pyi/Mipad DP Touch.app
./make_dmg.sh       # 生成 DMG 安装包
```

## 文档

- [INSTALL.md](INSTALL.md)：安装、权限授权与卸载说明。
- [平板侧说明](tablet/README.md)：模块安装、热重启与卸载说明。
- [CHANGELOG.md](CHANGELOG.md)：版本更新记录。
- [官网](https://inyvn.github.io/mipad-dp-touch/)：操作教程。

## 许可

本项目使用 **MIT License**，见 [LICENSE](LICENSE)。
