"""Unit tests for the dashboard backend (stdlib unittest; no extra deps).

Run:  python3 -m unittest discover -s tests -v   (from the jarvis-dashboard folder)
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["PATH"] = str(HERE / "fake_bin") + os.pathsep + os.environ["PATH"]
_settings = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_settings.close()
os.unlink(_settings.name)
os.environ["JARVIS_DASHBOARD_SETTINGS"] = _settings.name

import server  # noqa: E402

SAMPLE_VOICES = """\
Alex                en_US    # Most people recognize me by my voice.
Daniel              en_GB    # Hello! My name is Daniel.
Eddy (English (UK)) en_GB    # Hello! My name is Eddy.
Thomas              fr_FR    # Bonjour, je m’appelle Thomas.
"""


class ParsingTests(unittest.TestCase):
    def test_parse_voices_handles_spaces_and_parens(self):
        voices = server.parse_say_voices(SAMPLE_VOICES)
        self.assertEqual([v["name"] for v in voices], ["Alex", "Daniel", "Eddy (English (UK))", "Thomas"])
        self.assertEqual(voices[1]["lang"], "en_GB")

    def test_default_voice_prefers_daniel(self):
        voices = server.parse_say_voices(SAMPLE_VOICES)
        self.assertEqual(server.pick_default_voice(voices, "Daniel"), "Daniel")
        self.assertEqual(server.pick_default_voice(voices, "Karen"), "Daniel")
        no_daniel = [v for v in voices if v["name"] != "Daniel"]
        self.assertEqual(server.pick_default_voice(no_daniel, "Daniel"), "Alex")

    def test_settings_validation(self):
        s = server.save_settings({"rate": "9999", "muted": 1, "bogus": "x", "model": "qwen3.5:9b"})
        self.assertEqual(s["rate"], 360)
        self.assertIs(s["muted"], True)
        self.assertNotIn("bogus", s)
        self.assertEqual(server.load_settings()["model"], "qwen3.5:9b")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        server.Handler.port = cls.port
        server.Handler.oj_url = "http://127.0.0.1:9"  # nothing listens here
        server.Handler.ollama_url = "http://127.0.0.1:9"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_index_served(self):
        status, data = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<canvas id=\"orb\"", data)

    def test_path_traversal_blocked(self):
        status, _ = self.req("GET", "/../server.py")
        self.assertEqual(status, 404)

    def test_dns_rebinding_blocked(self):
        status, _ = self.req("GET", "/api/settings", headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(status, 403)

    def test_cross_origin_post_blocked(self):
        status, _ = self.req("POST", "/api/speak", {"text": "hi"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_proxy_reports_unreachable_openjarvis(self):
        status, data = self.req("GET", "/oj/v1/models")
        self.assertEqual(status, 502)
        self.assertIn(b"unreachable", data)

    def test_voices_from_say(self):
        status, data = self.req("GET", "/api/voices")
        d = json.loads(data)
        self.assertEqual(d["engine"], "macos-say")
        self.assertEqual(d["default"], "Daniel")
        self.assertTrue(d["daniel_available"])

    def test_speak_and_stop(self):
        log = tempfile.NamedTemporaryFile(delete=False, suffix=".log").name
        os.environ["FAKE_SAY_LOG"] = log
        try:
            result = {}

            def speak():
                result["r"] = self.req("POST", "/api/speak", {
                    "text": "one two three four five six seven eight nine ten eleven twelve",
                    "voice": "Daniel", "rate": 200})

            t = threading.Thread(target=speak)
            t.start()
            time.sleep(0.5)
            self.req("POST", "/api/stop", {})
            t.join(5)
            self.assertEqual(json.loads(result["r"][1])["status"], "stopped")
            lines = Path(log).read_text().splitlines()
            self.assertTrue(lines[0].startswith("START\tDaniel\t200\tone two"), lines)
            self.assertTrue(any(l.startswith("STOPPED") for l in lines), lines)

            status, data = self.req("POST", "/api/speak", {"text": "Short one.", "voice": "Daniel"})
            self.assertEqual(json.loads(data)["status"], "done")
        finally:
            os.environ.pop("FAKE_SAY_LOG", None)


if __name__ == "__main__":
    unittest.main()
