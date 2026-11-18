# Jarvis Dashboard

A full-screen, dark, voice-first personal assistant for OpenJarvis on your Mac:
an animated electric-blue orb that changes when Jarvis is **listening**,
**thinking** or **speaking**, with push-to-talk, a text box, a transcript, mute
and stop. Spoken replies use macOS `say`, with Daniel as the default voice.

A **Today** panel and a spoken **☀ Briefing** cover your schedule, Canvas
deadlines, payments due, subscription renewals and important email. All of it
is read-only.

Everything runs on your Mac and binds to `127.0.0.1` only.

```
browser ──► dashboard server (127.0.0.1:8770, this folder, Python stdlib)
              ├─ /oj/*  ─► OpenJarvis API, `jarvis serve` (127.0.0.1:8765) ─► Ollama (127.0.0.1:11434)
              │            chat: /v1/chat/completions · mic: /v1/speech/transcribe
              │            briefing: /api/digest/generate (official morning digest)
              ├─ /api/today, /api/briefing ─► read-only sources (assistant/):
              │     iCal feeds (Canvas, calendars) · IMAP inbox (EXAMINE) · bills.toml · Mac Calendar
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

## Personal assistant: Today and Briefing

### What it reads (all read-only)

| Source | How | Credentials |
|---|---|---|
| **Canvas** | The course calendar feed (iCal) | The feed URL is private, so it's kept in the macOS Keychain |
| **Calendar** | Any private iCal address (Google "secret address", iCloud public link). Optionally the **Mac Calendar** app's local database, through OpenJarvis' `apple_calendar` connector | URL in the Keychain. The Mac Calendar option needs no sign-in, only Full Disk Access |
| **Email** | IMAP: opens INBOX with `EXAMINE` (read-only) and fetches with `BODY.PEEK`, so messages stay unread | An **app password**, kept in the Keychain |
| **Bills & subscriptions** | `~/.jarvis-dashboard/bills.toml`, a file you edit | none |

* **Every item shows its source and when it was last checked.** The Sources list
  shows each source's status, and the briefing says when sources were checked.
* **Estimates are marked:** ≈ plus an ESTIMATE badge, with the reason. Possible
  reasons are an amount you marked `estimate = true`, a date projected forward from
  a monthly or yearly schedule, or an amount or date read from email text. The
  briefing says "about … estimated" out loud.
* **Email and calendar text is treated as data.** It's shown as plain text, never
  HTML. The spoken briefing is put together by code, not the model, so every
  amount and date is exactly what the source said. When Today is shared with the
  chat model, it's fenced off as untrusted data with instructions never to follow
  it. You can switch that off with **Use Today in chat**.
* **No action controls.** There's nothing to pay, transfer, cancel, reply or send.
  The IMAP wrapper can only log in, examine, search and fetch.
* **Credentials never leave your Mac.** Secrets are typed into your own terminal
  and go straight to the login Keychain (service `jarvis-dashboard`). Non-secret
  settings are in `~/.jarvis-dashboard/sources.json`.
* **Bank linking isn't part of this version.** Payments come from your list and
  from email.

### Set up sources (on your Mac, in Terminal)

```bash
cd ~/jarvis-dashboard
./jarvis-dashboard bills                  # creates ~/.jarvis-dashboard/bills.toml and opens it in TextEdit
./jarvis-dashboard setup canvas           # paste the Canvas Calendar Feed URL (hidden), tested, saved to Keychain
./jarvis-dashboard setup email            # address + app password (hidden), read-only test login, saved to Keychain
./jarvis-dashboard setup calendar --label Personal   # optional: a private iCal address
./jarvis-dashboard setup apple-calendar   # optional: the Mac Calendar app (needs Full Disk Access)
./jarvis-dashboard sources                # check everything now
./jarvis-dashboard remove email           # forget a source (also deletes its Keychain item)
```

Sources are re-checked every 15 minutes, or immediately with **Refresh** or
**☀ Briefing**. The old OpenJarvis morning digest is still available as
**OpenJarvis digest** under the Today panel.

## Start at login (optional LaunchAgent)

```bash
./jarvis-dashboard launchagent install    # writes ~/Library/LaunchAgents/local.jarvis-dashboard.plist and starts it
./jarvis-dashboard launchagent status
./jarvis-dashboard launchagent logs       # ~/Library/Logs/JarvisDashboard/launchagent.log (+ logs/ in this folder)
./jarvis-dashboard launchagent restart    # e.g. after updating
./jarvis-dashboard launchagent uninstall  # stop it and remove it
```

It runs this same launcher with no browser window, serving
`http://127.0.0.1:8770`. If it crashes, or Ollama isn't up yet, launchd retries
every 30 s. If Jarvis is already running, it exits quietly.

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
| Briefing | **☀ Briefing**: re-checks every source, then reads your Today briefing aloud. |
| Today | Left panel, or the **Today** tab in narrow windows: schedule, Canvas deadlines, payments, renewals, important email. |
| Full screen | **⛶** or **F**. |

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

* **A source shows ▲.** Run `./jarvis-dashboard sources` to see the error. Gmail
  needs IMAP enabled and an app password. A Canvas feed URL stops working if you
  reset it in Canvas; run `setup canvas` again.

## Tests

```bash
python3 -m unittest discover -s tests -v                      # backend + assistant tests (sample data)
OPENJARVIS_PYTHON=~/.openjarvis/.venv/bin/python tests/run-e2e.sh   # needs node + playwright
```

The end-to-end test starts the real launcher and the real OpenJarvis API server.
It replaces only Ollama (`tests/mock_ollama.py`), speech-to-text
(`tests/serve_with_mock_stt.py`, registered in OpenJarvis' speech registry),
`say`, `security` and `launchctl` (`tests/fake_bin/`). The assistant sources use
sample data from `tests/sample_servers.py`: Canvas and calendar iCal feeds, a
fake IMAP server that refuses and logs any write command, a bills list and a
Mac Calendar database. Chromium's fake microphone supplies the audio. It
does not touch your real config, because it uses a temporary `OPENJARVIS_HOME`.
