#!/usr/bin/env python3
"""Render real isolated AppKit source/frozen settings, never install or start HID."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Reuse verified renders from this unchanged build')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / 'renders.json'
    existing = json.loads(manifest.read_text()) if args.resume and manifest.exists() else []
    verified = {item['name']: item for item in existing
                if Path(item['png']).exists()
                and hashlib.sha256(Path(item['png']).read_bytes()).hexdigest() == item['sha256']}
    results = []
    executables = {
        'source': [str(root / '.venv/bin/python'), str(root / 'src/dptouch.py')],
        'frozen': [str(root / 'dist-pyi/Mipad DP Touch.app/Contents/MacOS/MipadDPTouch')],
    }
    for build, executable in executables.items():
        for appearance in ('light', 'dark'):
            cases = [(page, False, reduced, False) for page in ('overview', 'touch', 'gestures', 'display', 'runtime') for reduced in (False, True)]
            cases += [('runtime', True, False, False), ('display', False, False, True)]
            for page, advanced, reduced, panel in cases:
                name = f'{build}-{appearance}-' + ('mode-panel' if panel else 'advanced' if advanced else page) + ('-reduced' if reduced else '')
                if name in verified:
                    results.append(verified[name])
                    print(name, 'REUSED', flush=True)
                    continue
                png = output / f'{name}.png'
                log = output / f'{name}.log'
                log.unlink(missing_ok=True)  # App logs append; judge this run only.
                env = dict(os.environ, DPTOUCH_SELFTEST_WINDOW='shot:' + str(png),
                           DPTOUCH_SELFTEST_LOG=str(log), DPTOUCH_SHOT_DONE='1',
                           DPTOUCH_SHOT_APPEARANCE=appearance, DPTOUCH_SHOT_PAGE=page,
                           DPTOUCH_SHOT_ADVANCED='1' if advanced else '0',
                           DPTOUCH_SHOT_MODE_PANEL='1' if panel else '0',
                           DPTOUCH_SHOT_REDUCED='1' if reduced else '0')
                proc = subprocess.run(executable, cwd=root, env=env, timeout=90, capture_output=True)
                text = log.read_text() if log.exists() else proc.stderr.decode(errors='replace')
                assert proc.returncode == 0 and 'Traceback' not in text and '!!' not in text, (name, text[-4000:])
                assert '54 状态布局' in text and '可视反馈 AppKit 派发' in text
                assert '显示可视反馈: AppKit' in text and '常规可视反馈: AppKit' in text
                data = png.read_bytes()
                size = struct.unpack('>II', data[16:24])
                expected = (560, 438) if panel else (520, 340) if advanced else (980, 728)
                # AppKit bitmap scale follows the screen hosting the window.
                assert size in (expected, tuple(2 * value for value in expected)), (name, size)
                assert len(data) > 10000 and png.with_suffix('.png.pdf').exists()
                item = dict(name=name, png=str(png), dimensions=size, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), rc=proc.returncode)
                results.append(item)
                (output / 'renders.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(name, size, 'PASS', flush=True)
    assert len(results) == 48
    print('PASS 48 real AppKit renders + each run: five tabs, MIT once, 54-state matrix, native controls/panel dispatch, dynamic target/action previews')


if __name__ == '__main__':
    main()
