"""Opt-in macOS LaunchAgent: start the dashboard at login on 127.0.0.1:8770.

Nothing is installed unless you run `./jarvis-dashboard launchagent install`.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

LABEL = "local.jarvis-dashboard"
PORT = 8770


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "JarvisDashboard"


def _search_path() -> str:
    dirs: List[str] = []
    for tool in ("uv", "ollama", "say", "curl"):
        found = shutil.which(tool)
        if found:
            dirs.append(os.path.dirname(found))
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def build_plist(launcher: str) -> Dict:
    env = {
        "PATH": _search_path(),
        "JARVIS_NO_BROWSER": "1",
        "JARVIS_DASHBOARD_PORT": str(PORT),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    for key in ("OPENJARVIS_HOME", "OPENJARVIS_PYTHON", "JARVIS_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    logs = log_dir()
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", launcher],
        "WorkingDirectory": str(Path(launcher).parent),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # Restart if it crashes or Ollama wasn't ready yet; a clean exit
        # (e.g. "already running") is left alone.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(logs / "launchagent.log"),
        "StandardErrorPath": str(logs / "launchagent.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return "gui/%d" % os.getuid()


def run(action: str, launcher: str) -> int:
    path = plist_path()
    if action == "install":
        if not launcher or not Path(launcher).is_file():
            print("✗ launcher not found: %r" % launcher)
            return 1
        log_dir().mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _launchctl("bootout", "%s/%s" % (_domain(), LABEL))
        with open(path, "wb") as fh:
            plistlib.dump(build_plist(os.path.realpath(launcher)), fh)
        _launchctl("enable", "%s/%s" % (_domain(), LABEL))
        res = _launchctl("bootstrap", _domain(), str(path))
        if res.returncode != 0:
            print("✗ launchctl bootstrap failed: %s" % (res.stderr.strip() or res.stdout.strip()))
            return 1
        print("✓ Installed %s — Jarvis will start at login on http://127.0.0.1:%d" % (path, PORT))
        print("  It's starting now; logs: %s" % (log_dir() / "launchagent.log"))
        return 0
    if action == "uninstall":
        _launchctl("bootout", "%s/%s" % (_domain(), LABEL))
        if path.exists():
            path.unlink()
        print("✓ Removed the LaunchAgent. Jarvis won't start at login any more.")
        return 0
    if action == "restart":
        res = _launchctl("kickstart", "-k", "%s/%s" % (_domain(), LABEL))
        print("✓ Restarted." if res.returncode == 0 else "✗ %s" % (res.stderr.strip() or "not loaded"))
        return res.returncode
    if action == "status":
        if not path.exists():
            print("LaunchAgent not installed (opt-in: ./jarvis-dashboard launchagent install)")
            return 0
        res = _launchctl("print", "%s/%s" % (_domain(), LABEL))
        keep = [l.strip() for l in res.stdout.splitlines()
                if l.strip().startswith(("state =", "pid =", "last exit code =", "runs ="))]
        print("Installed: %s" % path)
        print("\n".join(keep) if keep else (res.stderr.strip() or "not loaded"))
        return 0
    if action == "logs":
        log = log_dir() / "launchagent.log"
        print("== %s ==" % log)
        if log.exists():
            print("".join(log.read_text(errors="replace").splitlines(keepends=True)[-60:]))
        else:
            print("(no log yet)")
        return 0
    return 1
