"""Secrets in the macOS login Keychain, via Apple's `security` tool.

Values are written with `security -i` (commands on stdin), so they don't show
up in the process list, then read back to confirm. Nothing is ever sent off
the Mac. On non-macOS systems (tests), set JARVIS_SECRETS_FILE to use a
0600 JSON file instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

SERVICE = "jarvis-dashboard"


class SecretStoreError(RuntimeError):
    pass


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _file_store() -> Optional[Path]:
    path = os.environ.get("JARVIS_SECRETS_FILE")
    return Path(path) if path else None


def _security() -> str:
    path = shutil.which("security")
    if not path:
        raise SecretStoreError(
            "macOS Keychain (`security`) not found. This feature runs on macOS."
        )
    return path


def get(key: str) -> Optional[str]:
    store = _file_store()
    if store:
        try:
            return json.loads(store.read_text()).get(key)
        except (OSError, ValueError):
            return None
    proc = subprocess.run(
        [_security(), "find-generic-password", "-s", SERVICE, "-a", key, "-w"],
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def set(key: str, value: str) -> None:  # noqa: A001 - mirrors get()
    if "\n" in value or "\r" in value:
        raise SecretStoreError("secret values can't contain line breaks")
    store = _file_store()
    if store:
        data = {}
        try:
            data = json.loads(store.read_text())
        except (OSError, ValueError):
            pass
        data[key] = value
        store.write_text(json.dumps(data))
        store.chmod(0o600)
        return
    security = _security()
    cmd = "add-generic-password -U -s %s -a %s -l %s -w %s\n" % (
        _quote(SERVICE), _quote(key), _quote("Jarvis Dashboard: " + key), _quote(value)
    )
    proc = subprocess.run([security, "-i"], input=cmd, capture_output=True,
                          text=True, timeout=20)
    if proc.returncode != 0 or get(key) != value:
        raise SecretStoreError(
            "Couldn't save to the Keychain: %s" % (proc.stderr.strip() or "value did not read back")
        )


def delete(key: str) -> None:
    store = _file_store()
    if store:
        try:
            data = json.loads(store.read_text())
        except (OSError, ValueError):
            return
        data.pop(key, None)
        store.write_text(json.dumps(data))
        return
    subprocess.run([_security(), "delete-generic-password", "-s", SERVICE, "-a", key],
                   capture_output=True, timeout=20)
