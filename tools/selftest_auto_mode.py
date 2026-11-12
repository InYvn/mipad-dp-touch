#!/usr/bin/env python3
"""Offline policy/Engine regression: never opens HID or posts live events."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dptouch_engine as E

class AutoMode(unittest.TestCase):
    def test_auto_version_interface_matrix(self):
        from dptouch_mode import resolve
        for version in ('26.9', '27.0', '27.2.9', '27.3', '27.3.1', '28.0', ''):
            is_modern = version in ('27.3', '27.3.1', '28.0')
            for touch in (False, True):
                for policy in ('auto', 'native', 'legacy'):
                    for confirmed in (False, True):
                        for ack in (False, True):
                            if not touch:
                                expected = 'pen_only'
                            elif policy == 'native':
                                expected = 'native'
                            elif policy == 'legacy':
                                expected = 'legacy' if (not is_modern or ack) else 'native'
                            else:
                                expected = 'native' if (is_modern and confirmed) else 'legacy'
                            with self.subTest(version=version, touch=touch, policy=policy,
                                              confirmed=confirmed, ack=ack):
                                self.assertEqual(
                                    resolve(policy, touch, version,
                                            legacy_ack=ack,
                                            native_confirmed=confirmed)['selected_mode'],
                                    expected)

    def test_auto_default_is_compat_on_modern_until_native_confirmed(self):
        """事故回归: macOS 27.3 + auto 盲选原生 = 手指完全没反应。"""
        from dptouch_mode import resolve
        # 默认 (未确认) 必须兼容模拟; 确认过才允许原生
        self.assertEqual(resolve('auto', True, '27.3')['selected_mode'], 'legacy')
        self.assertEqual(resolve('auto', True, '27.3', native_confirmed=True)['selected_mode'], 'native')
        # 显式手选仍照旧: 原生可用, 兼容模拟要 ack
        self.assertEqual(resolve('native', True, '27.3')['selected_mode'], 'native')
        self.assertEqual(resolve('legacy', True, '27.3', legacy_ack=True)['selected_mode'], 'legacy')
        self.assertEqual(resolve('legacy', True, '27.3')['selected_mode'], 'native')
        self.assertEqual(resolve('auto', True, '27.2')['selected_mode'], 'legacy')
        # 文案如实: 默认兼容模拟 + 怎么改成原生
        d = resolve('auto', True, '27.3')
        self.assertIn('原生触控尚未验证', d['mode_reason'])
        self.assertIn('兼容模拟', d['mode_reason'])
        self.assertIn('native_confirmed', d['native_hint'])
        self.assertFalse(d['native_touch'])
        self.assertIn('已确认原生可用', resolve('auto', True, '27.3', native_confirmed=True)['mode_reason'])

    def test_detection_before_first_report_and_pen(self):
        from selftest_native import Recorder, _finger_click, _pen_click
        rec = Recorder().install()
        with patch('platform.mac_ver', return_value=('27.3', (), '')):
            eng = E.Engine(log=lambda _: None)
            # 手选原生 (等价于设置窗口里点「原生触控」) 才允许 native 路径
            eng.update(touch_mode='native')
            self.assertEqual(eng.status()['selected_mode'], 'pen_only')
            _finger_click(eng)
            self.assertTrue(eng.touch_iface)
            self.assertEqual(eng.status()['selected_mode'], 'native')
            self.assertEqual(rec.ev, [])
            _pen_click(eng)
            self.assertTrue(rec.ev)

    def test_touch_remove_reconnect_clears_cookie_state(self):
        from selftest_native import Recorder
        Recorder().install()
        eng = E.Engine(log=lambda _: None)
        with patch.object(E, 'device_info', return_value=('tablet', 0x2717, 0x1234)), patch.object(E, 'first_finger_cookies', return_value={50, 60, 61}), patch.object(E, 'finger_cookie_map', return_value=[]):
            eng._device_cb(True)(None, 0, None, 123)
            self.assertTrue(eng.touch_iface)
            eng._device_cb(False)(None, 0, None, 456)  # unrelated pen removal
            self.assertTrue(eng.touch_iface)
            self.assertTrue(eng.devices, 'remaining touch interface must remain connected')
            eng._device_cb(False)(None, 0, None, 123)
            self.assertFalse(eng.touch_iface)
            self.assertEqual(eng._touch_cookies, set())
            self.assertEqual(eng._cookie2finger, {})
            eng._device_cb(True)(None, 0, None, 124)
            self.assertTrue(eng.touch_iface)

    def test_config_migration_and_effective_B_gate(self):
        import tempfile, json, types
        import dptouch as app
        with tempfile.TemporaryDirectory() as td, patch.object(app, 'CFG_PATH', td+'/config.json'), \
             patch.object(app, '_migrate_legacy_paths'), patch.object(app, 'log'):
            for raw, want in (({}, 'auto'), ({'native_touch': True}, 'auto'), ({'native_touch': False}, 'auto'), ({'native_touch': True, 'touch_mode': 'auto'}, 'auto'),
                              ({'touch_mode': 'legacy', 'legacy_touch_ack': True}, 'legacy'),
                              ({'touch_mode': 'legacy', 'legacy_touch_ack': True, 'native_confirmed': True}, 'legacy'),
                              ({'touch_mode': 'native', 'native_confirmed': True}, 'native'),
                              ({'touch_mode': 'bogus'}, 'auto')):
                Path(td+'/config.json').write_text(json.dumps(raw))
                cfg = app.load_cfg()
                self.assertEqual(cfg['touch_mode'], want)
                self.assertNotIn('native_touch', cfg)
                # 合法策略/确认状态不能被默认值重置 (重启持久化)
                if raw.get('touch_mode') in ('auto', 'native', 'legacy'):
                    self.assertEqual(cfg['touch_mode'], raw['touch_mode'])
                if 'legacy_touch_ack' in raw:
                    self.assertEqual(cfg['legacy_touch_ack'], bool(raw['legacy_touch_ack']))
                self.assertEqual(cfg['native_confirmed'], bool(raw.get('native_confirmed', False)))
        for native, touch in ((False, False), (True, False), (False, True), (True, True)):
            fake = types.SimpleNamespace(engine=types.SimpleNamespace(status=lambda: dict(native_touch=native, touch_iface=touch)))
            self.assertEqual(app.DPApp.native_touch_active(fake), native and touch)

    def test_disconnect_releases_legacy_drag(self):
        from selftest_native import Recorder, _finger_click
        rec = Recorder().install()
        eng = E.Engine(log=lambda _: None)
        _finger_click(eng)
        eng.update(touch_mode='legacy', legacy_touch_ack=True)
        eng.b.src_touch = True
        eng.b.down = eng.b.dragging = True
        with patch.object(E, 'device_info', return_value=('tablet', 0x2717, 0x1234)):
            eng._device_cb(False)(None, 0, None, 123)
        self.assertEqual(eng.status()['selected_mode'], 'pen_only')
        self.assertTrue(any(e[0] == 'mouse' and e[1] == 2 for e in rec.ev))
        self.assertFalse(eng.b.down)

    def test_policy_change_preserves_active_pen(self):
        from selftest_native import Recorder
        Recorder().install()
        eng = E.Engine(log=lambda _: None)
        eng.touch_iface = True
        eng.update(touch_mode='legacy', legacy_touch_ack=True)
        eng.b.src_touch = False
        eng.b.down = True
        eng._want_down = True
        eng.update(touch_mode='native')
        self.assertTrue(eng.b.down)
        self.assertTrue(eng._want_down)

    def test_engine_routing_matrix_and_no_inactivity_fallback(self):
        from selftest_native import Recorder, _finger_click, _pen_click
        from dptouch_mode import resolve
        for version in ('26.9', '27.0', '27.2', '27.3', '27.3.1', '28.0', ''):
            for policy in ('auto', 'native', 'legacy'):
                for acknowledged in (False, True):
                    for confirmed in (False, True):
                        with self.subTest(version=version, policy=policy, acknowledged=acknowledged,
                                          confirmed=confirmed), \
                             patch('platform.mac_ver', return_value=(version, (), '')):
                            rec = Recorder().install()
                            eng = E.Engine(log=lambda _: None)
                            eng.update(touch_mode=policy, legacy_touch_ack=acknowledged,
                                       native_confirmed=confirmed)
                            self.assertEqual(eng.status()['selected_mode'], 'pen_only')
                            _pen_click(eng)
                            self.assertTrue(any(e[0] == 'mouse' and e[1] == 1 for e in rec.ev))
                            rec.ev.clear()
                            _finger_click(eng)
                            want = resolve(policy, True, version, legacy_ack=acknowledged,
                                           native_confirmed=confirmed)
                            self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])
                            self.assertEqual(bool(rec.ev), not want['native_touch'])
                            with patch('time.time', return_value=1e20):
                                self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])
                                eng.update(gain=1.0)
                                self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])

    def test_native_selection_records_capability_confirmation(self):
        """手选「原生触控」= 用户确认本机原生可用; 手选并确认兼容模拟 = 撤掉该确认。"""
        import types
        import dptouch as app
        fake = types.SimpleNamespace(
            cfg={}, enabled=True,
            edid=types.SimpleNamespace(reconfigure=lambda: None,
                                       set_desired=lambda *a: None, tick=lambda: None))
        fake._apply = lambda **kw: fake.cfg.update(kw)
        fake.native_touch_active = lambda: True
        with patch('platform.mac_ver', return_value=('27.3', (), '')), patch.object(app, 'log'):
            self.assertTrue(app.DPApp.set_touch_mode(fake, 'native'))
            self.assertEqual(fake.cfg['touch_mode'], 'native')
            self.assertTrue(fake.cfg['native_confirmed'])
            # 切回自动不清除确认状态 (否则用户每次都得重新手选原生)
            self.assertTrue(app.DPApp.set_touch_mode(fake, 'auto'))
            self.assertEqual(fake.cfg['touch_mode'], 'auto')
            self.assertTrue(fake.cfg['native_confirmed'])
            self.assertFalse(fake.cfg['legacy_touch_ack'])
            # 手选并确认兼容模拟 -> 撤掉原生确认, 自动模式回到兼容模拟
            self.assertTrue(app.DPApp.set_touch_mode(fake, 'legacy', acknowledged=True))
            self.assertEqual(fake.cfg['touch_mode'], 'legacy')
            self.assertTrue(fake.cfg['legacy_touch_ack'])
            self.assertFalse(fake.cfg['native_confirmed'])

    def test_legacy_ack_required_in_backend(self):
        import dptouch as app
        import types
        fake = types.SimpleNamespace()
        with patch('platform.mac_ver', return_value=('27.3', (), '')):
            self.assertFalse(app.DPApp.set_touch_mode(fake, 'legacy'))
        self.assertFalse(app.DPApp.set_touch_mode(fake, 'invalid'))

    def test_stop_releases_legacy_drag_before_state_reset(self):
        from selftest_native import Recorder, _finger_click
        rec = Recorder().install()
        eng = E.Engine(log=lambda _: None)
        _finger_click(eng)
        eng.update(touch_mode='legacy', legacy_touch_ack=True)
        eng.b.src_touch = True
        eng.b.down = eng.b.dragging = True
        eng.stop()
        self.assertTrue(any(e[0] == 'mouse' and e[1] == 2 for e in rec.ev))
        self.assertFalse(eng.touch_iface)
        self.assertFalse(eng.b.down)

    def test_switch_clears_contacts_after_release(self):
        from selftest_native import Recorder
        Recorder().install()
        eng = E.Engine(log=lambda _: None)
        eng.touch_iface = True
        eng.update(touch_mode='legacy', legacy_touch_ack=True)
        eng.b.src_touch = True
        eng._fstate = [dict(down=True, x=0.3, y=0.7)]
        eng.update(touch_mode='native')
        self.assertEqual(eng._fstate, [dict(down=False, x=None, y=None)])

if __name__ == '__main__':
    unittest.main()
