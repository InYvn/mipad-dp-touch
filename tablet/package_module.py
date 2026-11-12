#!/usr/bin/env python3
"""Package the verified module bytes without rebuilding native payloads."""
from pathlib import Path
import argparse
import hashlib
import stat
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'module'
NAME = 'mipad-dp-touch-module-v2.1.4-os4.0.16.zip'
# Pinned after the deterministic build; --expect overrides it for the first
# build / for verifying an artifact captured out of band.
EXPECTED_SHA256 = '0cf94190932f8454d9194d03b3b96a38ff99e43a55925d22bfa719b54705e803'
MEMBERS = [
    'module.prop', 'customize.sh', 'post-fs-data.sh', 'service.sh',
    'uninstall.sh', 'action.sh', 'profile.sh', 'common.sh', 'platform.sh',
    'bootstrap.sh', 'runtime-monitor.sh', 'module-controller', 'platform-tool',
    'prepare-owner', 'fullsplit-bounded-v2.so', 'skip_mount', 'README.md',
    'manifest.json', 'runtime.sha256', 'installed.sha256', 'files.sha256',
]
EXECUTABLES = {'module-controller', 'platform-tool', 'prepare-owner'}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package(output, expected=None):
    contents = {}
    for name in MEMBERS:
        path = SOURCE / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or unsafe module member: ' + name)
        contents[name] = path.read_bytes()
    for manifest in ('files.sha256', 'installed.sha256', 'runtime.sha256'):
        seen = set()
        for line in contents[manifest].decode('ascii').splitlines():
            digest, name = line.split('  ', 1)
            if name in seen or name not in contents or sha256(contents[name]) != digest:
                raise ValueError('Integrity mismatch: ' + manifest + ': ' + name)
            seen.add(name)
    if set(contents) - {'files.sha256'} != {
        line.split('  ', 1)[1] for line in contents['files.sha256'].decode('ascii').splitlines()
    }:
        raise ValueError('Incomplete package manifest')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.pending')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in MEMBERS:
                mode = 0o755 if name.endswith('.sh') or name in EXECUTABLES else 0o644
                info = zipfile.ZipInfo(name, (2026, 11, 7, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, contents[name])
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or archive.namelist() != MEMBERS:
                raise ValueError('ZIP verification failed')
        digest = sha256(temporary.read_bytes())
        pinned = EXPECTED_SHA256 if expected is None else expected
        if pinned is not None and digest != pinned:
            raise ValueError('Package bytes differ from the verified release: ' + digest)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE / 'dist' / NAME)
    parser.add_argument('--expect', default=None,
                        help='expected SHA256 of the produced zip (default: the pinned release hash)')
    args = parser.parse_args()
    digest = package(args.output, args.expect)
    print('SHA256  ' + digest + '  ' + args.output.name)
    print('Bytes: ' + str(args.output.stat().st_size))
