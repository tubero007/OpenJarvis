#!/usr/bin/env bash
# End-to-end test: the real launcher + real OpenJarvis API server, with
#   * a mock Ollama (tests/mock_ollama.py),
#   * a mock speech-to-text backend registered in OpenJarvis (tests/serve_with_mock_stt.py),
#   * a fake `say` (tests/fake_bin/say),
#   * Chromium's fake microphone,
# driven by Playwright (tests/e2e.mjs). Needs: uv, node + playwright, an
# OpenJarvis venv (OPENJARVIS_PYTHON).
set -euo pipefail
T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
D="$(dirname "$T")"
WORK="${E2E_WORK:-$(mktemp -d)}"
mkdir -p "$WORK/ojhome" "$WORK/shots"
OLLAMA_PORT=11998 API_PORT=18766 DASH_PORT=18771

cat > "$WORK/ojhome/config.toml" <<EOF
[engine]
default = "ollama"
[engine.ollama]
host = "http://127.0.0.1:$OLLAMA_PORT"
[intelligence]
default_model = "qwen3.5:2b"
preferred_engine = "ollama"
[speech]
backend = "mock-stt"
[telemetry]
enabled = false
EOF

export OPENJARVIS_HOME="$WORK/ojhome"
export MOCK_OLLAMA_LOG="$WORK/ollama.jsonl" MOCK_STT_LOG="$WORK/stt.jsonl" FAKE_SAY_LOG="$WORK/say.log"
export MOCK_FIRST_TOKEN_DELAY=1.5 MOCK_STT_TEXT="What word did I ask you to remember?"
export JARVIS_DASHBOARD_SETTINGS="$WORK/settings.json"
: > "$MOCK_OLLAMA_LOG"; : > "$MOCK_STT_LOG"; : > "$FAKE_SAY_LOG"; rm -f "$JARVIS_DASHBOARD_SETTINGS"

python3 "$T/mock_ollama.py" "$OLLAMA_PORT" & MOCK_PID=$!
cleanup() { kill "$MOCK_PID" ${LAUNCH_PID:+"$LAUNCH_PID"} ${SAMPLE_PID:+"$SAMPLE_PID"} 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$OLLAMA_PORT/api/tags" >/dev/null 2>&1 && break; sleep 0.2; done

# Sample assistant data: iCal feeds + fake IMAP server + bills + Mac Calendar db,
# and a fake Keychain (tests/fake_bin/security backed by a JSON file).
export JARVIS_HOME="$WORK/jarvis-home" FAKE_KEYCHAIN="$WORK/jarvis-home/fake-keychain.json"
FEED_PORT=18903 IMAP_PORT=18904
rm -rf "$JARVIS_HOME"
python3 "$T/sample_servers.py" "$JARVIS_HOME" "$FEED_PORT" "$IMAP_PORT" > "$WORK/sample-servers.log" 2>&1 & SAMPLE_PID=$!
for _ in $(seq 1 50); do grep -q ready "$WORK/sample-servers.log" 2>/dev/null && break; sleep 0.2; done

# Exercise the launcher's setup commands exactly as on the Mac.
echo "launcher subcommands:"
printf 'http://127.0.0.1:%s/personal.ics\n' "$FEED_PORT" | PATH="$T/fake_bin:$PATH" OPENJARVIS_PYTHON="${OPENJARVIS_PYTHON:-}" \
  "$D/jarvis-dashboard" setup calendar --label "School events" | sed 's/^/  | /'
PATH="$T/fake_bin:$PATH" "$D/jarvis-dashboard" sources | sed 's/^/  | /'
PATH="$T/fake_bin:$PATH" "$D/jarvis-dashboard" remove "School events" | sed 's/^/  | /'
mkdir -p "$WORK/fakehome"
HOME="$WORK/fakehome" FAKE_LAUNCHCTL_LOG="$WORK/launchctl.log" PATH="$T/fake_bin:$PATH" \
  OPENJARVIS_PYTHON="${OPENJARVIS_PYTHON:-}" "$D/jarvis-dashboard" launchagent install | sed 's/^/  | /'
HOME="$WORK/fakehome" FAKE_LAUNCHCTL_LOG="$WORK/launchctl.log" PATH="$T/fake_bin:$PATH" \
  OPENJARVIS_PYTHON="${OPENJARVIS_PYTHON:-}" "$D/jarvis-dashboard" launchagent uninstall | sed 's/^/  | /'
sed 's/^/  | launchctl /' "$WORK/launchctl.log"

# The one-command launcher, exactly as on the Mac, pointed at the mocks.
PATH="$T/fake_bin:$PATH" OLLAMA_HOST_URL="http://127.0.0.1:$OLLAMA_PORT" \
  JARVIS_API_PORT=$API_PORT JARVIS_DASHBOARD_PORT=$DASH_PORT JARVIS_NO_BROWSER=1 \
  JARVIS_SERVE_ENTRY="$T/serve_with_mock_stt.py" \
  "$D/jarvis-dashboard" > "$WORK/launcher.log" 2>&1 & LAUNCH_PID=$!

for _ in $(seq 1 300); do
  curl -fsS "http://127.0.0.1:$DASH_PORT/api/settings" >/dev/null 2>&1 && break
  kill -0 "$LAUNCH_PID" 2>/dev/null || { cat "$WORK/launcher.log"; exit 1; }
  sleep 1
done
echo "launcher output:"; sed 's/^/  | /' "$WORK/launcher.log"

DASH_URL="http://127.0.0.1:$DASH_PORT" WORK="$WORK" IMAP_LOG="$JARVIS_HOME/imap-commands.log" node "$T/e2e.mjs"
echo "artifacts in $WORK"
