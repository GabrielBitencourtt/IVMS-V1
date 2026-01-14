# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\trabalhos\\IVMS-V1\\camera-scanner\\app.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\trabalhos\\IVMS-V1\\camera-scanner\\stream_bridge.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\websocket_server.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\ffmpeg_installer.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\cloud_agent.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\onvif_events.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\rtsp_tester.py', '.'), ('C:\\trabalhos\\IVMS-V1\\camera-scanner\\scanner.py', '.')],
    hiddenimports=['pystray', 'pystray._win32', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'websockets', 'websockets.server', 'websockets.client', 'requests'],
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
    a.binaries,
    a.datas,
    [],
    name='CameraScannerAgent',
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
    uac_admin=True,
    icon=['C:\\trabalhos\\IVMS-V1\\camera-scanner\\icon.ico'],
)
