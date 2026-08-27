"""Runtime-visible release identity for source and PyInstaller builds."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


SOFTWARE_VERSION = "2.4.0"
SWEEP_BACKEND_VERSION = "native-optimetrics-v3.2"
CONFIG_SCHEMA_VERSION = 3


def _metadata_path() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "config" / "build_metadata.json"
    return Path(__file__).resolve().parents[1] / "config" / "build_metadata.json"


def get_build_info() -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    path = _metadata_path()
    if path.is_file():
        try:
            # Windows PowerShell 5 writes UTF-8 JSON with a BOM.  Accept both
            # BOM and non-BOM metadata so a frozen release never falls back to
            # the misleading "development-source" identity.
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            payload = {}
    return {
        "software_version": str(payload.get("software_version", SOFTWARE_VERSION)),
        "build_time_utc": str(payload.get("build_time_utc", "development-source")),
        "source_hash": str(payload.get("source_hash", "development-source")),
        "sweep_backend_version": str(payload.get("sweep_backend_version", SWEEP_BACKEND_VERSION)),
        "config_schema_version": int(payload.get("config_schema_version", CONFIG_SCHEMA_VERSION)),
    }


def build_identity_line() -> str:
    info = get_build_info()
    return (
        "version={software_version} | build_time={build_time_utc} | "
        "source_hash={source_hash} | sweep_backend={sweep_backend_version} | "
        "config_schema={config_schema_version}"
    ).format(**info)
