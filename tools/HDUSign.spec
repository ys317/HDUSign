# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import sys

IS_WIN = sys.platform == 'win32'
FFMPEG_BIN = 'ffmpeg.exe' if IS_WIN else 'ffmpeg'

ROOT = Path(SPECPATH).resolve().parent
BROWSERS = ROOT / 'build' / 'portable-runtime' / 'browsers'
FFMPEG = ROOT / 'build' / 'portable-runtime' / 'ffmpeg'
if not BROWSERS.is_dir():
    raise RuntimeError('Use tools/release.py build --ffmpeg-bundle <audio archive> to prepare the portable runtime.')
if not all((FFMPEG / name).is_file() for name in (
        FFMPEG_BIN, 'LICENSE', 'README.txt', 'SOURCE.json', 'version.txt',
        'buildconf.txt', 'license-notice.txt')):
    raise RuntimeError('The complete FFmpeg runtime and notices are missing. '
                       'Use tools/release.py build --ffmpeg-bundle <audio archive> to prepare them.')

datas = [(str(ROOT / 'config.example.yaml'), '.'),
         (str(ROOT / 'assets' / 'hdusign.ico'), 'assets'),
         (str(ROOT / 'hdusign' / 'qml'), 'hdusign/qml'),
         (str(BROWSERS), 'browsers'),
         (str(FFMPEG), 'ffmpeg')]
if (ROOT / 'assets' / 'hdusign.png').is_file():
    datas.append((str(ROOT / 'assets' / 'hdusign.png'), 'assets'))
binaries = []
hiddenimports = ['soundcard.mediafoundation'] if IS_WIN else ['soundcard.pulseaudio']
for package in ('sherpa_onnx', 'soundcard', 'fastembed', 'onnxruntime',
                'tokenizers', 'huggingface_hub', 'certifi'):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

# These optional system libraries are pulled in by the build environment on
# Windows but make QtCore prefer an incompatible ICU/OpenSSL pair.
_qt_conflicting_dlls = {'icudt78.dll', 'icuuc.dll', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll'}
if IS_WIN:
    binaries = [entry for entry in binaries if Path(entry[0]).name.lower() not in _qt_conflicting_dlls]


a = Analysis(
    [str(ROOT / 'hdusign' / '__main__.py')],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / 'tools' / 'hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'customtkinter', 'hdusign.gui', 'hdusign.ui',
              'PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
    optimize=0,
)
# PyInstaller's dependency analysis can add the same optional libraries again
# after the explicit ``binaries`` list is filtered above (the build runtime
# exposes a Poppler directory on its search path).  Remove those auto-collected
# copies from the final TOC as well so Qt resolves the Windows ICU pair that it
# was built against instead of the incompatible Poppler ICU 78 exports.
if IS_WIN:
    a.binaries = [entry for entry in a.binaries
                  if Path(entry[0]).name.lower() not in _qt_conflicting_dlls]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HDUSign',
    icon=str(ROOT / 'assets' / 'hdusign.ico') if IS_WIN else None,
    version=str(ROOT / 'build' / 'windows-version.txt') if IS_WIN else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=IS_WIN,
    console=not IS_WIN,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=IS_WIN,
    upx_exclude=['ffmpeg.exe'] if IS_WIN else [],
    name='HDUSign',
)
