#!/usr/bin/env python3
"""Jarvis Dashboard backend.

A small, dependency-free (stdlib only) local web server that:

* serves the dashboard UI,
* proxies ``/oj/*`` to the OpenJarvis API server (``jarvis serve``), streaming
  responses straight through, so the browser only ever talks to one origin,
* speaks text with macOS ``say`` (selectable voice, interruptible),
* lists installed models by merging OpenJarvis ``/v1/models`` with Ollama's
  ``/api/tags`` (which only lists models whose download finished).

It binds to 127.0.0.1 only and rejects requests whose Host/Origin are not this
server, so other websites open in your browser cannot drive it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from assistant import briefing as briefing_mod  # noqa: E402
from assistant.today import TodayService  # noqa: E402

TODAY = TodayService()
STATIC_DIR = HERE / "static"
SETTINGS_PATH = Path(
    os.environ.get("JARVIS_DASHBOARD_SETTINGS", str(HERE / "settings.json"))
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "model": "qwen3.5:2b",
    "voice": "Daniel",
    "rate": 185,
    "muted": False,
}

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 25 * 1024 * 1024  # audio uploads for push-to-talk


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(update: Dict[str, Any]) -> Dict[str, Any]:
    settings = load_settings()
    for key, value in update.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key == "rate":
            try:
                value = max(90, min(360, int(value)))
            except (TypeError, ValueError):
                continue
        elif key == "muted":
            value = bool(value)
        elif not isinstance(value, str) or len(value) > 200:
            continue
        settings[key] = value
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings


# --------------------------------------------------------------------------
# macOS speech (say)
# --------------------------------------------------------------------------

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}[_-][A-Za-z0-9]+)\s+#")


def parse_say_voices(output: str) -> List[Dict[str, str]]:
    """Parse ``say -v '?'`` output into ``[{name, lang}]``."""
    voices = []
    for line in output.splitlines():
        match = _VOICE_LINE.match(line.rstrip())
        if match:
            voices.append(
                {"name": match.group("name").strip(), "lang": match.group("lang")}
            )
    return voices


def pick_default_voice(voices: List[Dict[str, str]], preferred: str) -> str:
    names = [v["name"] for v in voices]
    for candidate in (preferred, "Daniel"):
        if candidate in names:
            return candidate
    for v in voices:
        if v["lang"].startswith("en"):
            return v["name"]
    return names[0] if names else ""


class Speaker:
    """Runs one ``say`` process at a time; ``stop()`` interrupts it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._epoch = 0
        self._voices_cache: Optional[List[Dict[str, str]]] = None

    @property
    def say_path(self) -> Optional[str]:
        return shutil.which("say")

    def voices(self) -> List[Dict[str, str]]:
        if self._voices_cache is None:
            say = self.say_path
            if not say:
                return []
            try:
                out = subprocess.run(
                    [say, "-v", "?"], capture_output=True, text=True, timeout=15
                ).stdout
            except (OSError, subprocess.SubprocessError):
                return []
            self._voices_cache = parse_say_voices(out)
        return self._voices_cache

    def speak(self, text: str, voice: str, rate: int) -> str:
        """Speak *text*; block until finished. Returns done|stopped|error."""
        say = self.say_path
        if not say:
            return "unavailable"
        text = text.strip()
        if not text:
            return "done"
        cmd = [say, "-r", str(int(rate))]
        if voice:
            cmd += ["-v", voice]
        cmd += ["-f", "-"]  # read text from stdin: no argv length/escaping issues
        with self._lock:
            epoch = self._epoch
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError:
                return "error"
            self._proc = proc
        try:
            _, err = proc.communicate(text.encode("utf-8"), timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            return "error"
        with self._lock:
            if self._proc is proc:
                self._proc = None
            stopped = self._epoch != epoch
        if stopped:
            return "stopped"
        if proc.returncode != 0:
            sys.stderr.write("say failed: %s\n" % err.decode("utf-8", "replace"))
            return "error"
        return "done"

    def stop(self) -> None:
        with self._lock:
            self._epoch += 1
            proc = self._proc
            self._proc = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def fetch_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost
        return json.loads(resp.read().decode("utf-8"))


def list_models(oj_url: str, ollama_url: str) -> Dict[str, Any]:
    """Installed chat models: OpenJarvis' list, annotated with Ollama sizes.

    Ollama's /api/tags only lists models whose pull completed, so a model that
    is still (or was partially) downloading never appears here.
    """
    result: Dict[str, Any] = {"models": [], "errors": []}
    oj_ids: List[str] = []
    try:
        oj_ids = [m["id"] for m in fetch_json(oj_url + "/v1/models").get("data", [])]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("OpenJarvis /v1/models: %s" % exc)

    tags: Dict[str, Dict[str, Any]] = {}
    try:
        for m in fetch_json(ollama_url + "/api/tags").get("models", []):
            tags[m.get("name") or m.get("model")] = m
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("Ollama /api/tags: %s" % exc)

    ids = oj_ids or [
        name for name in tags if "embed" not in name.lower()
    ]  # fall back to Ollama directly
    for mid in ids:
        tag = tags.get(mid) or tags.get(mid + ":latest") or {}
        details = tag.get("details") or {}
        result["models"].append(
            {
                "id": mid,
                "size_bytes": tag.get("size"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "downloaded": bool(tag) if tags else None,
            }
        )
    return result


# --------------------------------------------------------------------------
# Request handler
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisDashboard/1.0"
    # set on the class by main()
    oj_url = "http://127.0.0.1:8765"
    ollama_url = "http://127.0.0.1:11434"
    speaker = Speaker()
    port = 8770

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        if os.environ.get("JARVIS_DASHBOARD_VERBOSE"):
            super().log_message(fmt, *args)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _allowed(self) -> bool:
        """Block DNS rebinding and cross-site requests."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            parsed = urllib.parse.urlparse(origin)
            if parsed.hostname not in LOCAL_HOSTS or parsed.port != self.port:
                return False
        return True

    # -- verbs --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed():
            return self._send_json({"error": "forbidden"}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/oj/"):
            return self._proxy()
        if path == "/api/settings":
            return self._send_json(load_settings())
        if path == "/api/voices":
            return self._voices()
        if path == "/api/models":
            return self._send_json(list_models(self.oj_url, self.ollama_url))
        if path == "/api/status":
            return self._status()
        if path in ("/api/today", "/api/briefing", "/api/today/context"):
            return self._today(path)
        return self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            return self._send_json({"error": "forbidden"}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/oj/"):
            return self._proxy()
        try:
            if path == "/api/settings":
                return self._send_json(save_settings(self._read_json()))
            if path == "/api/speak":
                data = self._read_json()
                settings = load_settings()
                status = self.speaker.speak(
                    str(data.get("text", ""))[:5000],
                    str(data.get("voice") or settings["voice"]),
                    int(data.get("rate") or settings["rate"]),
                )
                return self._send_json({"status": status})
            if path == "/api/stop":
                self.speaker.stop()
                return self._send_json({"status": "stopped"})
        except (ValueError, TypeError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json({"error": "not found"}, 404)

    # -- handlers -----------------------------------------------------------

    def _voices(self) -> None:
        voices = self.speaker.voices()
        settings = load_settings()
        self._send_json(
            {
                "engine": "macos-say" if self.speaker.say_path else None,
                "voices": voices,
                "default": pick_default_voice(voices, settings["voice"]),
                "daniel_available": any(v["name"] == "Daniel" for v in voices),
            }
        )

    def _status(self) -> None:
        status: Dict[str, Any] = {"openjarvis": False, "ollama": False}
        try:
            fetch_json(self.oj_url + "/health", timeout=2)
            status["openjarvis"] = True
        except Exception as exc:  # noqa: BLE001
            status["openjarvis_error"] = str(exc)
        try:
            status["speech_to_text"] = fetch_json(
                self.oj_url + "/v1/speech/health", timeout=5
            )
        except Exception as exc:  # noqa: BLE001
            status["speech_to_text"] = {"available": False, "reason": str(exc)}
        try:
            fetch_json(self.ollama_url + "/api/tags", timeout=2)
            status["ollama"] = True
        except Exception:  # noqa: BLE001
            pass
        status["tts"] = {
            "engine": "macos-say" if self.speaker.say_path else None,
            "speaking": self.speaker.speaking,
        }
        self._send_json(status)

    def _today(self, path: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        force = query.get("refresh", ["0"])[0] == "1"
        try:
            today = TODAY.build(force=force)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": "Today failed: %s" % exc}, 500)
        now = TODAY._now()
        if path == "/api/today":
            return self._send_json(today)
        if path == "/api/briefing":
            return self._send_json({"text": briefing_mod.compose(today, now),
                                    "generated_at": today["generated_at"]})
        return self._send_json({"context": briefing_mod.chat_context(today, now),
                                "generated_at": today["generated_at"]})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            return self._send_json({"error": "not found"}, 404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        """Forward /oj/<path> to the OpenJarvis server, streaming the reply."""
        upstream = urllib.parse.urlparse(self.oj_url)
        path = self.path[len("/oj") :]
        try:
            body = self._read_body() if self.command == "POST" else None
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 413)
        headers = {}
        for name in ("Content-Type", "Accept", "Authorization"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        if os.environ.get("OPENJARVIS_API_KEY") and "Authorization" not in headers:
            headers["Authorization"] = "Bearer " + os.environ["OPENJARVIS_API_KEY"]
        conn = http.client.HTTPConnection(
            upstream.hostname, upstream.port or 80, timeout=900
        )
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            return self._send_json(
                {"error": "OpenJarvis server unreachable: %s" % exc}, 502
            )
        try:
            self.send_response(resp.status)
            for name in ("Content-Type", "Cache-Control", "X-Voice-Id"):
                if resp.getheader(name):
                    self.send_header(name, resp.getheader(name))
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser aborted (e.g. user hit stop)
        finally:
            conn.close()
        self.close_connection = True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis Dashboard server")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--openjarvis", default="http://127.0.0.1:8765")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)

    Handler.oj_url = args.openjarvis.rstrip("/")
    Handler.ollama_url = args.ollama.rstrip("/")
    Handler.port = args.port
    # Loopback only. There is deliberately no option to bind elsewhere.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print("Jarvis dashboard on http://127.0.0.1:%d" % args.port, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.speaker.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
