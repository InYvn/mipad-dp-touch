# DP-in callback guard (experimental)

KernelSU module + recovery helper for the **DP-in black screen with backlight** on Xiaomi Pad
devices (tested device: Xiaomi Pad 9 Pro Max / `yingtian`, OS 4.0.16, KernelSU).

> **Status: experimental, not yet proven on a real black-screen boot.**
> This is not presented as a finished fix. The only recovery observed to work on this device
> was a full reboot (fresh `system_server` + fresh displayfeature HAL). See
> [Limitations](#limitations) before installing.

## Problem

When the tablet is used as a DP-in monitor, the display can stay black while the panel is on
and the Mac continues to render normally. Kernel log shows:

```text
dp-in: IRQ: Video ready
dp-in: Video: 3408x2272@120Hz, MIPI format:0x0, color depth:0x1
```

HAL log shows `displaycount: Received DP-IN ... status=0`, but the app never receives
`Display params changed` and never starts its capture session.

Root cause on this MIUI build: `DisplayFeatureManagerService` (DfMS) registers the
display-feature HAL callback only once, in `onBootPhase(500)`. If the HAL AIDL service is not
published at that moment, `initWrapperLocked()` returns silently and the callback is never
registered again. The HAL then broadcasts DP-IN parameters to zero consumers. There are two
distinct failure classes:

| Class | Symptom | Recoverable by this module? |
|---|---|---|
| `BROKEN_A` | `displaycount status=0`, no `receive dp in info` / `Display params changed` | Only for the **next** framework start; current boot needs a framework restart or reboot |
| `BROKEN_B` | `displaycount status=0x0`, `dp-in: Video ready timeout` | No — DP RX must be renegotiated by a physical USB-C replug or reboot |

## What the module does

`mipad_dpin_guard` (module id `mipad_dpin_guard`) runs after `sys.boot_completed` and watches
for a new `system_server` process. If a new framework starts while the displayfeature HAL is
not published, it starts the HAL with `ctl.start vendor.displayfeatureaidl-hal` before DfMS
reaches phase 500. Actions are bounded:

- at most 3 HAL starts per framework start and 10 per boot;
- 10 s cooldown between starts;
- a 45 s phase-500 guard window and a 75 s verification window;
- HAL start failures are logged and stop after the bound; no binder/framework patching.

The module also provides `dpin-recover.sh` for classification and controlled recovery. It does
**not** stop/start the full framework, does not touch `/odm`, HID, UDC, i2c, or the touch
module, and does not write any partition.

## Install

### KernelSU Manager (recommended)

```sh
cd tablet/module_guard
sh build-guard-zip.sh          # produces out/mipad_dpin_guard-0.1.0.zip
```

Install the zip from KernelSU Manager (local install), then reboot once. The module is a plain
KernelSU module layout (`module.prop` at zip root).

### ADB / root

From the repo root:

```sh
sh tablet/module_guard/install/install-guard.sh
# or, with a device serial:
SERIAL=<serial> sh tablet/module_guard/host/dpin-recover-host.sh install --start
```

The installer backs up any existing module directory, copies the payload, seeds
`/data/adb/mipad_dpin_guard/config`, runs the module self-test, and prints the rollback path.

## Usage

On the Mac, with `adb` and the tablet connected:

```sh
H=tablet/module_guard/host/dpin-recover-host.sh
$H status                 # classification + recent evidence lines
$H guard status           # guard daemon state
$H check-chain --timeout 60
$H snapshot               # screenshots + logs + props, read-only
$H recover --yes          # only acts if the current state is safely recoverable
$H logs                   # module event/verify logs
```

On the device (root):

```sh
sh /data/adb/modules/mipad_dpin_guard/bin/dpin-recover.sh status
sh /data/adb/modules/mipad_dpin_guard/bin/dpin-recover.sh check-chain
```

Configuration lives at `/data/adb/mipad_dpin_guard/config`. The important keys:

| Key | Default | Meaning |
|---|---|---|
| `ENABLE` | `1` | `0` disables the daemon |
| `HAL_WAIT` | `12` | seconds to wait for HAL publication after `ctl.start` |
| `PHASE500_GUARD` | `45` | warn after this many seconds without registration evidence |
| `ALLOW_FRAMEWORK_RESTART` | `0` | second key for the opt-in `framework-restart` helper |
| `EARLY_START` | `0` | experimental post-fs-data HAL start; leave off |

`framework-restart` requires `ALLOW_FRAMEWORK_RESTART=1` **and** `--yes`, is limited to once
per boot, and keeps the HAL running. It is intended only for a current boot that is already
`BROKEN_A`; it is never automatic.

## Limitations

- **Real-device validation is incomplete.** Host tests pass, but the guard was not running on
  the boot where DP-in recovered; that boot succeeded after a full reboot. Do not install this
  expecting it to repair an already-black boot.
- The current boot cannot be repaired by starting the HAL: DfMS has already passed phase 500
  and does not re-register. Use the opt-in framework restart or a reboot.
- If the HAL restarts *after* phase 500, the module can only detect and warn; it cannot make
  DfMS re-register.
- `BROKEN_B` (DP RX not trained) is out of scope; use a physical USB-C replug or reboot.
- The module only reduces a boot-time race; it is not a substitute for the OEM HAL/framework
  behavior and should be treated as a stopgap until a vendor-side fix exists.

### Known testing pitfall

Do **not** run `dumpsys media.camera` while a DP-in stream is active. It was followed within
100 ms by the vendor camera provider (`vendor_xring`) dying and the preview freezing. Capture
state with screenshots and `logcat`, not camera dumps.

## Uninstall / rollback

```sh
sh tablet/module_guard/install/uninstall-guard.sh
```

The uninstaller moves the module directory to a timestamped `removed-*` backup and stops the
daemon; it never deletes files. If you used `install-guard.sh`, the printed backup path can be
restored directly.

## Layout

```text
tablet/module_guard/
├── module/                 # KernelSU payload
│   ├── module.prop
│   ├── post-fs-data.sh
│   ├── service.sh
│   ├── uninstall.sh
│   ├── config/config
│   └── bin/{dpin-lib,dpin-guard,dpin-recover}.sh
├── build-guard-zip.sh
├── host/dpin-recover-host.sh
├── install/{install-guard,uninstall-guard}.sh
└── test/run-tests.sh
```

## Tests

Host-side only (no device, no root required):

```sh
cd tablet/module_guard
sh test/run-tests.sh
```

The suite covers module layout, configuration gates, zip reproducibility, installer rollback,
classification, recovery refusal for `BROKEN_B`, and framework-restart double-key behavior.
