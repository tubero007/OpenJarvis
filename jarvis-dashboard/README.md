# Jarvis Dashboard

A full-screen, dark, voice-first dashboard for OpenJarvis on your Mac: an animated
electric-blue orb that changes when Jarvis is **listening**, **thinking** or
**speaking**, with push-to-talk, a text box, a transcript, mute and stop. Spoken
replies use macOS `say`, with Daniel as the default voice.

Everything runs on your Mac and binds to `127.0.0.1` only.

```
browser ──► dashboard server (127.0.0.1:8770, this folder, Python stdlib)
              ├─ /oj/*  ─► OpenJarvis API, `jarvis serve` (127.0.0.1:8765) ─► Ollama (127.0.0.1:11434)
              │            chat: /v1/chat/completions · mic: /v1/speech/transcribe
              │            briefing: /api/digest/generate (official morning digest)
              ├─ /api/speak, /api/stop ─► macOS `say -v Daniel`
              └─ /api/models ─► OpenJarvis /v1/models + Ollama /api/tags (sizes)
```

## Your install stays untouched

* This lives in its own folder (e.g. `~/jarvis-dashboard`). Nothing is written to
  `~/.openjarvis` except what `jarvis serve` itself normally writes (telemetry,
  memory). The OpenJarvis config is only read.
* `jarvis serve` needs `fastapi`/`uvicorn`, and mic transcription needs
  `faster-whisper`, which the one-line installer doesn't put in your venv. The
  launcher layers them on with `uv run --with …`, which uses a cached overlay
  environment, so `~/.openjarvis/.venv` is not modified.
* The API server runs on port **8765**, so it doesn't clash with a `jarvis serve`
  you may run yourself on 8000.
* Dashboard preferences (model, voice, rate, mute) go to `settings.json` in this
  folder.

## Run it

```bash
cd ~/jarvis-dashboard
./jarvis-dashboard doctor   # inspect: OpenJarvis, Ollama models (fully downloaded only), voices
./jarvis-dashboard          # start everything and open http://127.0.0.1:8770
```

Press Ctrl-C in that terminal to stop. Logs are in `logs/`. The first start
takes a minute while uv downloads the server packages.

## Using it

| Action | How |
|---|---|
| Talk | Hold **Hold to talk**, or hold **Space** when the text box isn't focused. Release to send. Talking interrupts Jarvis. |
| Type | Type in the box and press Enter. Typed and spoken turns share one conversation. |
| Stop speaking | **■ Stop** or **Esc**. This also cancels a reply that's still generating. |
| Mute | **🔊 Voice on / 🔇 Muted**. Replies still appear as text. |
| Voice | **Voice** menu: every installed `say` voice, English first. Daniel by default. |
| Model | **Model** menu: models Ollama has finished downloading, with size. |
| Compare | **Compare**: runs the same three prompts on each model and reports load time, time to first token, tokens/s, total time, a reasoning check and the full answers. **Make default** saves the choice. |
| Briefing | **☀ Briefing**: runs the OpenJarvis morning digest and reads it aloud. |
| Full screen | **⛶** or **F**. |

## Morning briefing and accounts

The Briefing button calls OpenJarvis' own digest endpoint
(`POST /api/digest/generate`, which runs the `morning_digest` agent). It uses
**only sources that are already connected**. The dashboard never connects
anything. With nothing connected, the briefing will be short. Connecting Gmail,
Calendar, Oura and so on is a separate step, using `jarvis connect` or the
OpenJarvis settings, and it's your call.

The digest uses OpenJarvis' configured default model (`[intelligence]
default_model`), not the model picked in the dashboard.

## Troubleshooting

* **Mic STT light is red.** `faster-whisper` didn't load. Check `logs/openjarvis.log`.
  The first transcription downloads the Whisper `base` model (~145 MB) from
  Hugging Face.
* **The browser never asks for the microphone.** macOS System Settings → Privacy &
  Security → Microphone → enable your browser. Then allow the mic for
  `127.0.0.1:8770` in the browser's site settings.
* **No Daniel.** System Settings → Accessibility → Spoken Content → System Voice →
  Manage Voices → English (UK) → Daniel.
* **A model is missing from the menu.** Ollama lists a model only after its download
  finishes. Run `./jarvis-dashboard doctor`, which also flags leftover partial
  downloads, then `ollama pull <model>` to finish it.

## Tests

```bash
python3 -m unittest discover -s tests -v                      # backend unit tests
OPENJARVIS_PYTHON=~/.openjarvis/.venv/bin/python tests/run-e2e.sh   # needs node + playwright
```

The end-to-end test starts the real launcher and the real OpenJarvis API server.
It replaces only Ollama (`tests/mock_ollama.py`), speech-to-text
(`tests/serve_with_mock_stt.py`, registered in OpenJarvis' speech registry), and
`say` (`tests/fake_bin/say`). Chromium's fake microphone supplies the audio. It
does not touch your real config, because it uses a temporary `OPENJARVIS_HOME`.
