"""Minimal mock of the Ollama HTTP API, for tests only.

Implements the endpoints OpenJarvis' Ollama engine uses (/api/tags,
/api/chat with streaming NDJSON, /api/version, /api/show, /api/ps) and logs
every chat request to a JSONL file so tests can inspect the exact messages
(system prompt, conversation history, model) that OpenJarvis sent.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_OLLAMA_LOG", "/dev/null")
FIRST_DELAY = float(os.environ.get("MOCK_FIRST_TOKEN_DELAY", "0.8"))
TOKEN_DELAY = float(os.environ.get("MOCK_TOKEN_DELAY", "0.03"))

MODELS = [
    ("qwen3.5:2b", 2_700_000_000, "2.3B"),
    ("qwen3.5:9b", 6_600_000_000, "9.7B"),
    ("nomic-embed-text:latest", 274_000_000, "137M"),
]
COUNTER = {"n": 0}


def reply_for(model: str, messages: list) -> str:
    system = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if "briefing" in system.lower() or "digest" in user.lower() or "collected_data" in user:
        if "score" in system.lower() and "evaluat" in system.lower():
            return '{"score": 8, "feedback": ""}'
        return "Good morning. Your sources are quiet today, which I choose to read as a compliment."
    if "single word" in user.lower():
        return "ready"
    if "bat and a ball" in user.lower():
        return "The ball costs 5 cents." if "9b" in model else "The ball costs 10 cents."
    COUNTER["n"] += 1
    if "long" in user.lower():
        return " ".join(
            "This is sentence number %d of a deliberately long answer." % i for i in range(1, 9)
        )
    return "Mock reply %d from %s. You said: %s. Noted, with my usual composure." % (
        COUNTER["n"], model, user.strip().rstrip("."),
    )


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            return self._json({"models": [
                {"name": n, "model": n, "size": s,
                 "details": {"parameter_size": p, "quantization_level": "Q4_K_M",
                             "family": "nomic-bert" if "embed" in n else "qwen35"}}
                for n, s, p in MODELS]})
        if self.path.startswith("/api/version"):
            return self._json({"version": "0.0.0-mock"})
        if self.path.startswith("/api/ps"):
            return self._json({"models": []})
        if self.path in ("/", ""):
            self.send_response(200); self.end_headers(); self.wfile.write(b"Ollama is running")
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/api/show"):
            return self._json({"capabilities": ["completion"], "model_info": {}, "details": {}})
        if not self.path.startswith("/api/chat"):
            return self._json({"error": "not found"}, 404)
        model = body.get("model", "")
        messages = body.get("messages", [])
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "model": model, "messages": messages,
                                 "stream": body.get("stream"), "think": body.get("think")}) + "\n")
        if model not in [m[0] for m in MODELS]:
            return self._json({"error": "model '%s' not found" % model}, 404)
        text = reply_for(model, messages)
        if not body.get("stream", True):
            time.sleep(FIRST_DELAY)
            return self._json({"model": model, "message": {"role": "assistant", "content": text},
                               "done": True, "prompt_eval_count": 10, "eval_count": len(text.split())})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        time.sleep(FIRST_DELAY)
        words = text.split(" ")
        for i, w in enumerate(words):
            piece = w if i == 0 else " " + w
            self.wfile.write((json.dumps({"model": model, "message": {"role": "assistant", "content": piece}, "done": False}) + "\n").encode())
            self.wfile.flush()
            time.sleep(TOKEN_DELAY)
        self.wfile.write((json.dumps({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                                      "prompt_eval_count": 10, "eval_count": len(words)}) + "\n").encode())
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
