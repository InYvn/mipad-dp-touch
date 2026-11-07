# 原生 EDID B 助手

派生自 **arlint/vedid**，MIT，Copyright (c) 2026 Austin Arlint。完整许可证见 `LICENSE`；许可证也嵌入二进制，可用 `vedid license` 读取。这里只使用 RAM-only 的 `IOAVServiceSetVirtualEDIDMode` 私有 SPI，不写 EEPROM，不使用模式/排列/镜像/旋转设置 API。

## 构建与只读验证

在仓库根目录：

```sh
make -B -C tools/native
codesign --verify --strict tools/native/build/vedid
tools/native/build/vedid selftest
.venv/bin/python tools/selftest_edid.py
```

产物 `tools/native/build/vedid` 为 arm64+x86_64 universal、ad-hoc 签名；Rosetta 下拒绝执行。实际显示覆盖路径只预期用于 Apple Silicon。Makefile 将警告视为错误。App 打包由上层 `build_app.sh` 负责，不由本目录安装或启用任何覆盖。

只读命令：

- `help` / `--help`、`license` / `--license`、`selftest`。
- `list`：在线屏身份与几何；`snapshot`：全屏模式、逻辑/物理像素、刷新率、原点、旋转、镜像。
- `readback --display ID`：该屏当前几何及模式。
- `dump --display ID [--out FILE|-]`：AV EDID；默认 stdout JSON 的 `edid` 是 base64。
- `validate --edid FILE`：离线长度/头/扩展计数/每块 checksum 校验。
- `check-patch --factory FILE --edid FILE`：离线、独立重建 B 后逐字节比较。
- `check-factory --factory FILE`：离线检查已支持的工厂固件 profile。
- `preflight --display ID --factory FILE --edid FILE --vendor 25001 --product 4 --name 'MI DISPLAY' --native 3408x2272`：运行真实解析和所有前置安全检查，返回 `stage=ready, readonly=true`；**不会调用任何 set/reset**。

`selftest` 不枚举、不写显示器；默认 Python 自检也不启动 guard。缺失原生二进制将使 Python 自检失败，不再静默跳过。

## 有写入能力的唯一命令

**仅在单独获准的真机验证或 App 用户启用时执行：**

```text
guard --display ID --factory FACTORY_FILE --edid B_FILE \
  --vendor 25001 --product 4 --name 'MI DISPLAY' --native 3408x2272
```

不提供无生命周期保护的 `apply` / `reset`，这两个入口总是拒绝。`--factory` 与全部身份参数都是 guard 必需项；旧版不带 factory 的直接调用必须更新。

安全边界：

1. 非内建屏、CG vendor/product/serial 与 EDID 完整身份一致；名称与首选时序精确匹配；同型号在线屏必须唯一。
2. 根据 CoreDisplay location 解析控制器范围，并用 AV EDID 完整身份过滤。Apple Silicon 上 framebuffer shim 和 AV epic 可能不在同一直接子树，因此按同一设备族作用域和控制器 epic 名对应；不使用遍历中“遇到位置之后随便选第一个”的状态。多个服务命中时拒绝。
3. 在原 AV handle 上再次比较调用方工厂文件全部字节；要求当前有效尺寸与工厂声明相符。只支持归档验证过的 384B 固件 profile：对序列号/生产日期/base checksum 归零后比较 SHA-256。**不是锁定某个序列号**，但新固件也会保守拒绝，需要独立审查后扩充 profile。
4. 原生边界独立重建 **282×188 mm、base 28×19 cm** 的 B，逐字节相等才接受。合法 CTA DTD start=0 表示没有 DTD，跳过；身份、时序、扩展和其他字节不能改。
5. SIGTERM/SIGINT/SIGHUP/SIGPIPE 处理安装在写入前。guard 保留最初的 IORegistry service 和 IOAV handle；EOF/信号后只可能通过该 handle 恢复，必须仍是原 registry ID，且当前 EDID 是自己的 payload。已恢复的 factory 不再次 reset；断连/身份改变/第三方 payload 则不写。
6. apply 必须同时通过 EDID、有效尺寸、原服务和全屏配置快照回读。恢复必须回读 factory 字节和原尺寸。不能把 SPI 成功码当成功。
7. stdout 每阶段一个 JSON：第一行 `applied` / `apply-failed` / `refused`；已发生写入时最终有 `restored`。`restored=true` 只在回读确认后出现。失败输出含 API rc、EDID/几何/服务/配置检查结果、前后快照；stdout 非阻塞，父进程不读输出也不能把恢复卡住。

Python 控制器 API 保持 `set_desired` / `tick` / `reconfigure` / `status` / `shutdown`。仅 app-enabled + native + correction 三者为真才启动 guard；稳定 tick 不重写。备份以完整设备身份和 factory SHA-256 分开命名，同一个支持目录可以服务后来接入的另一个序列号；同一身份的未知 EDID 变化拒绝。状态 JSON 保留原生 apply/restore 报告。stdout 由线程按完整行读取，调用方超时有界；stderr 持续排空并只保留最后 16 KiB。恢复超时不 SIGKILL、不丢失仍存活 guard 的所有权、不启动另一个 guard。

## 验证边界

本轮独立审查实际完成 universal clang 编译、签名、原生 selftest、离线 payload/profile 校验、真实 `dump`/`snapshot`/`preflight`、arm64 ASan+UBSan 自检与 preflight。**本轮没有执行任何真机 apply/reset/guard，也没有验证真实父进程崩溃后的硬件恢复。**

上游使用私有 SPI：覆盖后 `IOAVServiceCopyEDID` 是否反映有效 payload 必须由获准的真机试验确认。本实现严格要求一致，不会降级成只看尺寸就宣称 applied；若 SPI 只返回 factory，会报失败并尝试通过原 handle 立即恢复。SIGKILL、系统断电、内核/API 永久卡死不可由进程保证恢复；并发第三方写入也没有原子 compare-and-swap 契约。插拔、唤醒、多屏及真实触控效果仍需上层独立验收。
