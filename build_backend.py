"""Minimal dependency-free PEP 517/660 backend for this local-only CLI."""

import base64
import hashlib
import zipfile
from pathlib import Path

NAME = 'jobhunt-0.1.0'
INFO = NAME + '.dist-info'


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _wheel(wheel_directory, False)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _wheel(wheel_directory, True)


def _wheel(directory, editable):
    files = {
        INFO + '/METADATA': (
            b'Metadata-Version: 2.4\nName: jobhunt\nVersion: 0.1.0\n'
            b'Requires-Python: >=3.14\nLicense-Expression: MIT\nLicense-File: LICENSE\n'
        ),
        INFO + '/licenses/LICENSE': (Path(__file__).parent / 'LICENSE').read_bytes(),
        INFO + '/WHEEL': (
            b'Wheel-Version: 1.0\nGenerator: jobhunt-local\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
        ),
        INFO + '/entry_points.txt': b'[console_scripts]\njobhunt = jobhunt.cli:main\n',
    }
    if editable:
        files['jobhunt.pth'] = str(Path(__file__).parent.resolve() / 'src').encode() + b'\n'
    else:
        for path in (Path(__file__).parent / 'src/jobhunt').glob('*.py'):
            files['jobhunt/' + path.name] = path.read_bytes()
    rows = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
        rows.append(f'{name},sha256={digest},{len(content)}\n')
    files[INFO + '/RECORD'] = (''.join(rows) + INFO + '/RECORD,,\n').encode()
    name = NAME + '-py3-none-any.whl'
    with zipfile.ZipFile(Path(directory) / name, 'w') as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return name
