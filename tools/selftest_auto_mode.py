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
            for touch in (False, True):
                for policy in ('auto', 'native', 'legacy'):
                    expected = 'pen_only' if not touch else ('native' if policy == 'native' or (policy == 'auto' and version in ('27.3', '27.3.1', '28.0')) else 'legacy')
                    with self.subTest(version=version, touch=touch, policy=policy):
                        self.assertEqual(resolve(policy, touch, version, legacy_ack=True)['selected_mode'], expected)

    def test_detection_before_first_report_and_pen(self):
        from selftest_native import Recorder, _finger_click, _pen_click
        rec = Recorder().install()
        with patch('platform.mac_ver', return_value=('27.3', (), '')):
            eng = E.Engine(log=lambda _: None)
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
        with tempfile.TemporaryDirectory() as td, patch.object(app, 'CFG_PATH', td+'/config.json'), patch.object(app, '_migrate_legacy_paths'):
            for raw, want in (({}, 'auto'), ({'native_touch': True}, 'auto'), ({'native_touch': False}, 'auto'), ({'native_touch': True, 'touch_mode': 'auto'}, 'auto')):
                Path(td+'/config.json').write_text(json.dumps(raw))
                cfg = app.load_cfg()
                self.assertEqual(cfg['touch_mode'], want)
                self.assertNotIn('native_touch', cfg)
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
                    with self.subTest(version=version, policy=policy, acknowledged=acknowledged), patch('platform.mac_ver', return_value=(version, (), '')):
                        rec = Recorder().install()
                        eng = E.Engine(log=lambda _: None)
                        eng.update(touch_mode=policy, legacy_touch_ack=acknowledged)
                        self.assertEqual(eng.status()['selected_mode'], 'pen_only')
                        _pen_click(eng)
                        self.assertTrue(any(e[0] == 'mouse' and e[1] == 1 for e in rec.ev))
                        rec.ev.clear()
                        _finger_click(eng)
                        want = resolve(policy, True, version, acknowledged)
                        self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])
                        self.assertEqual(bool(rec.ev), not want['native_touch'])
                        with patch('time.time', return_value=1e20):
                            self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])
                            eng.update(gain=1.0)
                            self.assertEqual(eng.status()['selected_mode'], want['selected_mode'])

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
