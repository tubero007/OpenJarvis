"""Where the assistant keeps its (non-secret) config and data.

Secrets (IMAP app password, private iCal URLs) live in the macOS Keychain,
not here. See secrets.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


def home() -> Path:
    path = Path(os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis-dashboard")))
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def sources_path() -> Path:
    return home() / "sources.json"


def bills_path() -> Path:
    return home() / "bills.toml"


def load_sources() -> Dict[str, Any]:
    try:
        data = json.loads(sources_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_sources(data: Dict[str, Any]) -> None:
    path = sources_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
