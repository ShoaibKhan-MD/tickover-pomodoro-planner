# -*- mode: python ; coding: utf-8 -*-

import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Uvicorn loads protocol and event-loop implementations dynamically. Collect
# its submodules so packaged macOS builds don't fail only when a request hits
# a less-common protocol or lifespan path.
uvicorn_hiddenimports = collect_submodules('uvicorn')
multipart_hiddenimports = collect_submodules('multipart')
# The menu-bar / system-tray icon picks its platform backend at run time.
pystray_hiddenimports = collect_submodules('pystray') + ['PIL.Image']
if sys.platform == 'darwin':
    pystray_hiddenimports += ['objc', 'AppKit', 'Foundation', 'Quartz', 'PyObjCTools', 'PyObjCTools.AppHelper']

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')] + collect_data_files('tzdata'),
    hiddenimports=sorted(set(
        uvicorn_hiddenimports
        + multipart_hiddenimports
        + pystray_hiddenimports
        + [
            'uvicorn.logging',
            'uvicorn.loops.auto',
            'uvicorn.protocols.http.auto',
            'uvicorn.protocols.websockets.auto',
            'uvicorn.lifespan.on',
            'multipart.multipart',
            'starlette.formparsers',
            'tzdata',
        ]
    )),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Tickover',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    upx=False,
    upx_exclude=[],
    name='Tickover',
)
app = BUNDLE(
    coll,
    name='Tickover.app',
    icon='assets/Tickover.icns',
    bundle_identifier='com.shoaibkhan.tickover',
    info_plist={
        'CFBundleDisplayName': 'Tickover',
        'CFBundleShortVersionString': '1.3.0',
        'CFBundleVersion': '1.3.0',
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
    },
)
