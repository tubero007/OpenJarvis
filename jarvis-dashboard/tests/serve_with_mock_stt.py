"""Run the real `jarvis serve`, with a mock speech-to-text backend registered.

Tests only. faster-whisper needs to download Whisper weights from Hugging Face,
which the test environment can't reach, so this registers a "mock-stt" backend
in OpenJarvis' own SpeechRegistry (select it with `[speech] backend =
"mock-stt"` in config.toml). Everything else — routes, engine, streaming — is the
unmodified OpenJarvis server.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

from openjarvis.core.registry import SpeechRegistry
from openjarvis.speech._stubs import SpeechBackend, TranscriptionResult

LOG = os.environ.get("MOCK_STT_LOG", "/dev/null")
TEXT = os.environ.get("MOCK_STT_TEXT", "What did I just type to you?")


@SpeechRegistry.register("mock-stt")
class MockSTT(SpeechBackend):
    backend_id = "mock-stt"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def transcribe(self, audio: bytes, *, format: str = "wav",
                   language: Optional[str] = None) -> TranscriptionResult:
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"bytes": len(audio), "format": format,
                                 "head": audio[:4].hex()}) + "\n")
        return TranscriptionResult(text=TEXT, language="en", confidence=0.99,
                                   duration_seconds=1.5)

    def health(self) -> bool:
        return True

    def supported_formats(self) -> List[str]:
        return ["wav", "webm", "mp4", "ogg", "mp3"]


if __name__ == "__main__":
    import openjarvis.speech  # noqa: F401  (register built-ins first)
    from openjarvis.cli import main

    sys.argv = ["jarvis"] + sys.argv[1:]
    sys.exit(main())
