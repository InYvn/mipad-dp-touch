#!/usr/bin/env python3
"""Position defaults -> strict profile gate, fully isolated from live hardware."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dptouch as APP
import hid_bridge as HB
import selftest_edid as FIX


def main():
    count = 0
    # Hard-stop final injection boundary even if future code starts a bridge here.
    def forbidden(*args, **kwargs):
        raise AssertionError('test attempted real Quartz injection')
    with patch.object(HB.cg, 'CGEventPost', forbidden), \
         patch.object(HB.cg, 'CGEventCreateMouseEvent', return_value=None), \
         patch.object(HB.cg, 'CGEventSetIntegerValueField', forbidden), \
         tempfile.TemporaryDirectory() as td:
        config = Path(td) / 'config.json'
        with patch.object(APP, 'CFG_PATH', str(config)), patch.object(APP, '_migrate_legacy_paths'):
            for case, raw, unknown, state, writes in (
                ('verified default automatic', {}, False, 'applied', 1),
                ('unknown firmware refused', {}, True, 'failed', 0),
                ('manual off respected', {'edid_correct': False}, False, 'disabled', 0),
            ):
                config.write_text(json.dumps(raw))
                cfg = APP.load_cfg()
                env = FIX.FakeEnv()
                if unknown:
                    data = bytearray(env.factory)
                    data[150] ^= 1
                    data[255] = (-sum(data[128:255])) & 255
                    env.factory = bytes(data)
                ctl, _ = FIX._mk_ctl(env, state_dir=str(Path(td) / str(count)))
                ctl.set_desired(True, True, cfg['edid_correct'])
                assert ctl.tick()['state'] == state, case
                assert len(env.calls) == writes, case
                assert json.loads(config.read_text()) == raw, 'config must remain untouched'
                ctl.shutdown('isolated test')
                count += 1
                print('PASS', case)
    print('PASS %d isolated default/profile/manual policy cases' % count)


if __name__ == '__main__':
    main()
