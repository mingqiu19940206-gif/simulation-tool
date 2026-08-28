# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).parent
source_root = project_root / "src"
ansys_datas, ansys_binaries, ansys_hiddenimports = collect_all("ansys.aedt.core")

project_datas = [
    (str(project_root / "config" / "default_config.json"), "config"),
    (str(project_root / "config" / "build_metadata.json"), "config"),
]

analysis = Analysis(
    [str(source_root / "main_gui.py")],
    pathex=[str(source_root), str(project_root)],
    binaries=ansys_binaries,
    datas=project_datas + ansys_datas,
    hiddenimports=ansys_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "sphinx", "jupyter", "notebook"],
    noarchive=False,
    optimize=1,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="SurfaceCapAntipadTool_v2.4.0",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=str(project_root / "packaging" / "version_info.txt"),
)
