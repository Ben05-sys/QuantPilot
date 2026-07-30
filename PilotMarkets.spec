# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PilotMarkets.

    pip install pyinstaller
    pyinstaller PilotMarkets.spec

Produces dist/PilotMarkets.exe — a single windowed binary with the web UI
and the icon bundled in.

Two things worth knowing:

  - `--windowed` means no console. The terminal lives in the tray and the
    browser, so a console window would only ever be in the way. It also
    means print() goes nowhere: anything the user must see has to reach
    the UI.
  - yfinance pulls in a lot transitively, and PyInstaller's analysis
    misses the protobuf message classes that the websocket decoder builds
    at import time. They are named explicitly below; without them the exe
    runs but never receives a tick, which looks exactly like a quiet
    market.
"""

block_cipher = None

a = Analysis(
    ['pilot.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app/web', 'app/web'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        # The streaming stack. yfinance.live imports these lazily.
        'yfinance',
        'yfinance.live',
        'yfinance.pricing_pb2',
        'websockets',
        'websockets.sync',
        'websockets.sync.client',
        'google.protobuf',
        'google.protobuf.json_format',
        # zoneinfo needs the platform tz data at runtime; on Windows there
        # is no system tz database, so this must come along.
        'tzdata',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing here draws with matplotlib — the charts are canvas in
        # the browser — and it is the single largest thing pandas drags in.
        'matplotlib',
        'tkinter',
        'PIL',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='PilotMarkets',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/pilotmarkets.ico',
)
