#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平板连着时保持屏幕唤醒。

背景
----
Mac 锁屏后，外接的平板屏经常停在黑屏：系统没有“用户活动”就不会点亮外屏，
必须敲一下键盘才出现解锁界面。这里用系统自带的 caffeinate 做两件事：

1. 连上平板时先 `caffeinate -u -t 1` 声明一次用户活动 —— 把已经睡着的外屏点醒；
2. 之后长期跑 `caffeinate -d` —— 阻止显示器休眠，锁屏界面就一直留在平板上。

只影响“显示器休眠”，不声明持续活动，所以系统的自动锁屏计时照常走。
"""

import os
import subprocess

CAFFEINATE = "/usr/bin/caffeinate"


class WakeKeeper:
    """按“平板是否在线”维护一个 caffeinate -d 子进程。"""

    def __init__(self, log=None):
        self._proc = None
        self._log = log or (lambda _m: None)

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def sync(self, present, enabled=True):
        want = bool(present and enabled)
        if want and not self.running:
            self._start()
        elif not want and self._proc is not None:
            self.stop()

    def _start(self):
        try:
            # 先点醒：只声明 1 秒活动，不持续重置空闲计时
            subprocess.Popen([CAFFEINATE, "-u", "-t", "1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 再防睡：-d = 阻止显示器休眠；-w 绑本进程，App 退出它自己退
            self._proc = subprocess.Popen(
                [CAFFEINATE, "-d", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._log("唤醒: 已开始保持显示器唤醒 (caffeinate pid %d)"
                      % self._proc.pid)
        except Exception as e:
            self._proc = None
            self._log("唤醒: 启动 caffeinate 失败: %r" % (e,))

    def stop(self):
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.terminate()
            self._log("唤醒: 已停止保持唤醒")
        except Exception:
            pass

    def close(self):
        self.stop()
