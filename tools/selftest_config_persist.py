#!/usr/bin/env python3
"""离线回归: 触控策略与「用户确认」状态不会被默认值/整份保存覆盖。

钉住的行为 (对应现场故障: 设了 legacy+ack, 过一会配置又变回 auto/false):
  1. config.json 里合法的 touch_mode / legacy_touch_ack / native_confirmed 经
     load_cfg -> save_cfg -> load_cfg 原样保留 —— App 重启不重置用户选择。
  2. 老配置只有 native_touch 布尔值 -> touch_mode=auto (证明不了手动来源), 旧键被清掉;
     显式存过的合法策略不受影响。
  3. 非法值才回默认; 默认值里 native_confirmed 必须是 False (原生能力未验证)。
  4. App 运行中从外部改 config.json 的触控策略 -> _sync_policy_from_disk 采纳并生效,
     下一次 _apply 落盘也不会把它写回旧值 (整份 save_cfg 覆盖路径的回归)。
  5. 调用方显式改策略时, 显式值仍然优先于磁盘 (不能反过来把用户刚点的选择盖掉)。

用法: python3 tools/selftest_config_persist.py   (全过 exit 0, 失败 exit 1)
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dptouch as app  # noqa: E402


def _write(path, raw):
    Path(path).write_text(json.dumps(raw, ensure_ascii=False))


class FakeApp(object):
    """只带落盘/同步路径要用的属性, 不碰 AppKit / HID。"""

    POLICY_KEYS = app.DPApp.POLICY_KEYS

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.engine_calls = []
        self.engine = types.SimpleNamespace(update=self._engine_update)
        self._cfg_mtime = None

    def _engine_update(self, **kw):
        self.engine_calls.append(dict(kw))
        self.cfg.update(kw)

    def _reflect(self):
        pass

    def _sync_policy_from_disk(self, force=False):
        return app.DPApp._sync_policy_from_disk(self, force=force)

    def _apply(self, **kw):
        return app.DPApp._apply(self, **kw)


class CfgPersist(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = self._td.name + '/config.json'
        for p in (patch.object(app, 'CFG_PATH', self.path),
                  patch.object(app, '_migrate_legacy_paths'),
                  patch.object(app, 'log')):
            p.start()
            self.addCleanup(p.stop)

    def _load(self):
        cfg = app.load_cfg()
        self.assertIn('native_confirmed', cfg)
        return cfg

    def test_roundtrip_keeps_policy_and_acknowledgements(self):
        for raw in (dict(touch_mode='legacy', legacy_touch_ack=True, native_confirmed=False),
                    dict(touch_mode='native', legacy_touch_ack=False, native_confirmed=True),
                    dict(touch_mode='auto', legacy_touch_ack=True, native_confirmed=True)):
            with self.subTest(raw=raw):
                _write(self.path, raw)
                cfg = self._load()
                for key, want in raw.items():
                    self.assertEqual(cfg[key], want)
                app.save_cfg(cfg)                      # App 退出/任意设置动作落盘
                again = self._load()                   # App 重启
                for key, want in raw.items():
                    self.assertEqual(again[key], want)
                app.save_cfg(again)
                disk = json.loads(Path(self.path).read_text())
                for key, want in raw.items():
                    self.assertEqual(disk[key], want)

    def test_defaults_do_not_claim_native_capability(self):
        self.assertIs(app.DEFAULTS['native_confirmed'], False)
        _write(self.path, {})
        cfg = self._load()
        self.assertEqual(cfg['touch_mode'], 'auto')
        self.assertFalse(cfg['legacy_touch_ack'])
        self.assertFalse(cfg['native_confirmed'])

    def test_legacy_boolean_migration_does_not_pin_mode(self):
        for raw in ({'native_touch': True}, {'native_touch': False},
                    {'native_touch': True, 'legacy_touch_ack': True}):
            with self.subTest(raw=raw):
                _write(self.path, raw)
                cfg = self._load()
                self.assertEqual(cfg['touch_mode'], 'auto')
                self.assertNotIn('native_touch', cfg)
        # 显式存过的合法策略不受老布尔值迁移影响
        _write(self.path, {'native_touch': False, 'touch_mode': 'legacy',
                           'legacy_touch_ack': True, 'native_confirmed': False})
        cfg = self._load()
        self.assertEqual(cfg['touch_mode'], 'legacy')
        self.assertTrue(cfg['legacy_touch_ack'])
        self.assertFalse(cfg['native_confirmed'])

    def test_invalid_values_fall_back_without_touching_others(self):
        _write(self.path, {'touch_mode': 'nonsense', 'legacy_touch_ack': True,
                           'native_confirmed': True})
        cfg = self._load()
        self.assertEqual(cfg['touch_mode'], 'auto')
        self.assertTrue(cfg['legacy_touch_ack'])
        self.assertTrue(cfg['native_confirmed'])

    def test_running_app_adopts_external_policy_edit(self):
        cfg = self._load()
        self.assertEqual((cfg['touch_mode'], cfg['legacy_touch_ack']), ('auto', False))
        fake = FakeApp(cfg)
        _write(self.path, dict(cfg, touch_mode='legacy', legacy_touch_ack=True))
        self.assertTrue(fake._sync_policy_from_disk(force=True))
        self.assertEqual(fake.cfg['touch_mode'], 'legacy')
        self.assertTrue(fake.cfg['legacy_touch_ack'])
        self.assertEqual(fake.engine_calls[-1].get('touch_mode'), 'legacy')
        # mtime 没变 -> 不重复读/不重复推送
        fake.engine_calls.clear()
        self.assertFalse(fake._sync_policy_from_disk())

    def test_apply_preserves_external_edit_instead_of_clobbering(self):
        cfg = self._load()
        fake = FakeApp(cfg)
        _write(self.path, dict(cfg, touch_mode='legacy', legacy_touch_ack=True))
        saved = {}
        with patch.object(app, 'save_cfg', lambda c: saved.update(dict(c))):
            fake._apply(gain=1.25)
        self.assertEqual(saved.get('touch_mode'), 'legacy')
        self.assertTrue(saved.get('legacy_touch_ack'))
        self.assertEqual(saved.get('gain'), 1.25)
        self.assertFalse(saved.get('native_confirmed'))

    def test_apply_explicit_policy_change_wins_over_disk(self):
        cfg = self._load()
        fake = FakeApp(cfg)
        _write(self.path, dict(cfg, touch_mode='legacy', legacy_touch_ack=True))
        saved = {}
        with patch.object(app, 'save_cfg', lambda c: saved.update(dict(c))):
            fake._apply(touch_mode='auto', legacy_touch_ack=False)
        self.assertEqual(saved.get('touch_mode'), 'auto')
        self.assertFalse(saved.get('legacy_touch_ack'))


if __name__ == '__main__':
    unittest.main()
