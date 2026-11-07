#!/usr/bin/env python3
"""Offline view-model tests; no HID, display writes or event injection."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dptouch_window as UI

class Overview(unittest.TestCase):
    def state(self, **changes):
        st = dict(selected_mode='native', touch_mode='auto', running=True,
                  ax=True, rc=0, devices=[('tablet',)], touch_iface=True)
        st.update(changes)
        return st

    def test_effective_modes_and_provenance(self):
        for mode, title in [('pen_only', '仅笔'), ('native', '原生触控'), ('legacy', '兼容模拟')]:
            for policy, provenance in [('auto', '自动'), ('native', '手动'), ('legacy', '手动')]:
                with self.subTest(mode=mode, policy=policy):
                    main, detail = UI.input_mode_text(self.state(selected_mode=mode, touch_mode=policy), True)
                    self.assertIn(title, main)
                    self.assertIn('运行中', main)
                    self.assertIn(provenance, detail)

    def test_inactive_never_claims_running(self):
        for changes, enabled, reason in [({}, False, '已暂停'), ({'ax':False}, True, '等待授权'),
                ({'rc':-1}, True, '输入不可用'), ({'running':False}, True, '未运行'),
                ({'devices':[]}, True, '未连接')]:
            with self.subTest(changes=changes):
                main, detail = UI.input_mode_text(self.state(**changes), enabled)
                self.assertIn(reason, main)
                self.assertNotIn('运行中', main)
                self.assertIn('自动', detail)

    def test_unknown_mode_not_inferred_from_preference(self):
        main, _ = UI.input_mode_text(self.state(selected_mode=None), True)
        self.assertIn('等待检测', main)
        self.assertNotIn('原生', main)

if __name__ == '__main__':
    unittest.main()
