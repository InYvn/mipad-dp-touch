#!/usr/bin/env python3
"""Real AppKit control contracts; no HID, config or event posting."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from AppKit import NSApplication, NSSegmentedControl, NSSlider, NSMakeRect
import dptouch_window as UI

class NativeControls(unittest.TestCase):
    def test_direction_keeps_legacy_index_contract(self):
        NSApplication.sharedApplication()
        control = UI.direction_control(NSMakeRect(0,0,300,26))
        self.assertIsInstance(control, NSSegmentedControl)
        for i in range(3):
            control.selectItemAtIndex_(i)
            self.assertEqual(control.indexOfSelectedItem(), i)
    def test_grouped_actions_preserve_semantic_indices(self):
        import hid_bridge as HB
        for gesture, _, options, _ in HB.BIND_GESTURES:
            control = UI.action_control(NSMakeRect(0,0,250,26), gesture)
            self.assertFalse(control.isBordered())
            for i, action in enumerate(options):
                control.selectItemAtIndex_(i)
                self.assertEqual(control.indexOfSelectedItem(), i)
                self.assertEqual(control.titleOfSelectedItem(), HB.action_title(gesture, action))
            self.assertTrue(any(not item.isEnabled() for item in control.menu().itemArray()))

    def test_monitor_hit_regions_follow_real_global_bounds(self):
        from dptouch_previews import monitor_regions
        infos = [dict(id=11,bounds=(-1920,0,1920,1080)), dict(id=22,bounds=(0,-200,2560,1440))]
        regions = monitor_regions(infos, 600,150)
        self.assertEqual([key for key, _ in regions], [11,22])
        self.assertLess(regions[0][1][0], regions[1][1][0])
        self.assertEqual(monitor_regions([],600,150), [])

    def test_mode_list_selection_is_not_application(self):
        control = UI.mode_list(NSMakeRect(0,0,500,260))
        control.addItemsWithTitles_(['1704 × 1136 · HiDPI · 60 Hz', '1280 × 800 · HiDPI · 60 Hz'])
        control.selectItemAtIndex_(1)
        self.assertEqual(control.indexOfSelectedItem(), 1)
        self.assertEqual(control.titleOfSelectedItem(), '1280 × 800 · HiDPI · 60 Hz')
        self.assertIsNone(control.target())

    def test_gain_snaps_to_existing_gain_indices(self):
        control = UI.gain_control(NSMakeRect(0,0,180,24))
        self.assertIsInstance(control, NSSlider)
        self.assertTrue(control.allowsTickMarkValuesOnly())
        self.assertEqual(control.numberOfTickMarks(), 5)
        for i in range(5):
            control.selectItemAtIndex_(i)
            self.assertEqual(control.indexOfSelectedItem(), i)

if __name__ == '__main__': unittest.main()
