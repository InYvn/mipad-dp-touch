"""Regression for user-requested removal of calibration reassurance."""
from pathlib import Path
import unittest

class CopyTest(unittest.TestCase):
    def test_no_personal_calibration_reassurance(self):
        source = (Path(__file__).resolve().parents[1] / 'src/dptouch_window.py').read_text()
        for phrase in ('无需逐人标定', '不用逐人标定', '不需要逐人标定'):
            self.assertNotIn(phrase, source)

if __name__ == '__main__': unittest.main()
