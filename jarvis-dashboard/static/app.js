/* Jarvis Dashboard — client.
 *
 * Talks only to the dashboard server (same origin):
 *   /oj/*        -> OpenJarvis API (chat completions, speech transcription, digest)
 *   /api/*       -> dashboard helpers (macOS `say`, settings, installed models)
 */
"use strict";

const PERSONA = [
  "You are Jarvis, a personal AI assistant running entirely on the user's own Mac.",
  "Personality: calm, composed and quietly confident, with a subtle, dry wit that you use sparingly.",
  "Never sarcastic at the user's expense; helpful first, clever second.",
  "Your replies are usually spoken aloud, so: answer in one to three short sentences unless the user asks for detail;",
  "write plain conversational prose with no markdown, bullet lists, code blocks, emoji or URLs unless explicitly asked;",
  "don't narrate your reasoning; if you don't know something, say so briefly.",
  "In this chat you have no access to the user's email, calendar, files or the internet, so never claim to have checked them.",
].join(" ");

const HISTORY_LIMIT = 24; // messages kept in the model's context

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && typeof opts.body === "string" ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* not json */ }
  if (!res.ok) {
    const detail = data && (data.detail || data.error);
    const err = new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function stripThink(text) {
  return text.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").replace(/^\s+/, "");
}

/** Make text pleasant to hear: drop markdown, links, emoji. */
function speakable(text) {
  return text
    .replace(/```[\s\S]*?```/g, " (code omitted) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "a link")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*•]\s+/gm, "")
    .replace(/\*{1,3}([^*]+)\*{1,3}/g, "$1")
    .replace(/_{1,2}([^_]+)_{1,2}/g, "$1")
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Split off complete sentences from `text` starting at `from`.
 * A sentence ends at . ! ? or … followed by whitespace, so "qwen3.5" or
 * "$1.10" don't split. The trailing, still-streaming fragment is left alone. */
function takeSentences(text, from) {
  const out = [];
  const re = /[\s\S]*?[.!?…]+["'”’)\]]*\s+/y;
  re.lastIndex = from;
  let pos = from;
  let m;
  while ((m = re.exec(text)) && m[0].length) {
    out.push(m[0]);
    pos = re.lastIndex;
  }
  return { sentences: out, pos };
}

// ---------------------------------------------------------------- state
const app = {
  state: "idle",
  history: [], // {role, content}
  settings: { model: "qwen3.5:2b", voice: "Daniel", rate: 185, muted: false },
  ttsEngine: null, // "macos-say" | "browser" | null
  sttAvailable: null,
  chatAbort: null,
  busy: false,
  micLevel: 0,
};
window.jarvis = app; // handy for debugging / tests

const STATE_LABELS = {
  idle: "Standing by",
  listening: "Listening",
  transcribing: "Transcribing",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Something went wrong",
};

function setState(state, label) {
  app.state = state;
  document.body.dataset.state = state === "transcribing" ? "thinking" : state;
  $("state-label").textContent = label || STATE_LABELS[state] || state;
  (app.stateLog ||= []).push(state);
}

function settle() {
  // Return to idle unless something is still in progress.
  if (speech.active) return setState("speaking");
  if (app.busy) return setState("thinking");
  if (recorder.active) return setState("listening");
  setState("idle");
}

// ---------------------------------------------------------------- transcript
function addEntry(role, text, tag) {
  const li = document.createElement("li");
  li.className = role;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = role === "user" ? "You" : role === "assistant" ? "Jarvis" : "Note";
  if (tag) {
    const t = document.createElement("span");
    t.className = "tag";
    t.textContent = tag;
    who.appendChild(t);
  }
  const msg = document.createElement("div");
  msg.className = "msg";
  msg.textContent = text;
  li.append(who, msg);
  $("transcript").appendChild(li);
  li.scrollIntoView({ block: "end" });
  return { li, msg };
}

function note(text) { addEntry("system", text); }

// ---------------------------------------------------------------- speech out
const speech = {
  queue: [],
  active: false,
  epoch: 0,
  _wake: null,

  enqueue(text) {
    const clean = speakable(text);
    if (!clean || app.settings.muted || !app.ttsEngine) return;
    this.queue.push(clean);
    if (!this.active) this._run();
  },

  async _run() {
    this.active = true;
    const epoch = this.epoch;
    setState("speaking");
    while (this.queue.length && epoch === this.epoch) {
      const text = this.queue.shift();
      try {
        if (app.ttsEngine === "macos-say") {
          const r = await api("/api/speak", {
            method: "POST",
            body: JSON.stringify({ text, voice: app.settings.voice, rate: app.settings.rate }),
          });
          if (r.status === "error") note("macOS speech failed for that sentence.");
        } else if (app.ttsEngine === "browser") {
          await browserSpeak(text);
        }
      } catch (e) {
        note("Voice output failed: " + e.message);
        break;
      }
    }
    if (epoch === this.epoch) this.queue = [];
    this.active = false;
    settle();
  },

  async stop() {
    this.epoch++;
    this.queue = [];
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (app.ttsEngine === "macos-say") {
      try { await api("/api/stop", { method: "POST", body: "{}" }); } catch (_) { /* ignore */ }
    }
    this.active = false;
  },
};

function browserSpeak(text) {
  return new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    const v = speechSynthesis.getVoices().find((x) => x.name === app.settings.voice);
    if (v) u.voice = v;
    u.rate = app.settings.rate / 185;
    u.onend = u.onerror = () => resolve();
    speechSynthesis.speak(u);
  });
}

// ---------------------------------------------------------------- chat
/**
 * Stream a chat completion from OpenJarvis. Calls onText(visibleTextSoFar).
 * Returns {text, ttftMs, totalMs, chunks}.
 */
async function streamChat(messages, model, { signal, onText } = {}) {
  const t0 = performance.now();
  const res = await fetch("/oj/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, messages, stream: true, temperature: 0.6, max_tokens: 400 }),
    signal,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const d = await res.json(); detail = d.detail || d.error || detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "", raw = "", ttft = null, chunks = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") continue;
      let obj;
      try { obj = JSON.parse(payload); } catch (_) { continue; }
      if (obj.error) throw new Error(obj.error.message || JSON.stringify(obj.error));
      const delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
      const piece = delta && delta.content;
      if (piece) {
        if (ttft === null) ttft = performance.now() - t0;
        chunks++;
        raw += piece;
        // OpenJarvis reports engine failures (e.g. model not found) as streamed
        // text with HTTP 200; never show or speak that as Jarvis's reply.
        if (onText && !isEngineError(raw)) onText(stripThink(raw));
      }
    }
  }
  if (isEngineError(raw)) throw new Error(stripThink(raw).trim().replace(/^Error during generation:\s*/, ""));
  return { text: stripThink(raw).trim(), ttftMs: ttft, totalMs: performance.now() - t0, chunks };
}

function isEngineError(raw) {
  return /^Error during generation:/.test(stripThink(raw).trimStart());
}

async function converse(userText, source) {
  userText = userText.trim();
  if (!userText) return;
  if (app.chatAbort) app.chatAbort.abort();
  await speech.stop();

  addEntry("user", userText, source === "voice" ? "🎙 voice" : "⌨ typed");
  app.history.push({ role: "user", content: userText });

  const { li, msg } = addEntry("assistant", "", app.settings.model);
  li.classList.add("pending");
  const ctrl = new AbortController();
  app.chatAbort = ctrl;
  app.busy = true;
  setState("thinking");

  let spokenPos = 0;
  const myEpoch = speech.epoch;
  try {
    const messages = [{ role: "system", content: PERSONA }, ...app.history.slice(-HISTORY_LIMIT)];
    const result = await streamChat(messages, app.settings.model, {
      signal: ctrl.signal,
      onText(visible) {
        msg.textContent = visible;
        li.scrollIntoView({ block: "end" });
        if (speech.epoch !== myEpoch) return; // user pressed stop
        const { sentences, pos } = takeSentences(visible, spokenPos);
        spokenPos = pos;
        sentences.forEach((s) => speech.enqueue(s));
      },
    });
    msg.textContent = result.text || "(no reply)";
    if (speech.epoch === myEpoch) {
      const rest = result.text.slice(spokenPos);
      if (rest.trim()) speech.enqueue(rest);
    }
    app.history.push({ role: "assistant", content: result.text });
    app.lastTiming = result;
  } catch (e) {
    if (e.name === "AbortError") {
      msg.textContent = (msg.textContent || "") + " [stopped]";
      if (msg.textContent.trim() !== "[stopped]") {
        app.history.push({ role: "assistant", content: msg.textContent });
      }
    } else {
      li.remove();
      app.history.pop(); // drop the unanswered user turn from context
      note("Couldn't reach the model: " + e.message);
      setState("error");
      app.busy = false;
      if (app.chatAbort === ctrl) app.chatAbort = null;
      setTimeout(settle, 2500);
      return;
    }
  } finally {
    li.classList.remove("pending");
  }
  if (app.chatAbort === ctrl) app.chatAbort = null;
  app.busy = false;
  settle();
}

// ---------------------------------------------------------------- push to talk
const recorder = {
  active: false,
  held: false,
  stream: null,
  rec: null,
  chunks: [],
  startedAt: 0,
  audioCtx: null,
  analyser: null,

  async start() {
    if (this.held) return;
    this.held = true;
    $("ptt").classList.add("active");
    // Barge-in: talking over Jarvis stops it.
    await speech.stop();
    if (app.chatAbort) app.chatAbort.abort();
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch (e) {
      this.held = false;
      $("ptt").classList.remove("active");
      note("Microphone unavailable: " + e.message + ". Allow microphone access for this page in your browser settings.");
      return settle();
    }
    if (!this.held) return this._release(); // released before permission resolved

    const types = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"];
    const mimeType = types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
    this.rec = new MediaRecorder(this.stream, mimeType ? { mimeType } : undefined);
    this.chunks = [];
    this.rec.ondataavailable = (ev) => ev.data.size && this.chunks.push(ev.data);
    this.rec.start(250);
    this.startedAt = performance.now();
    this.active = true;
    this._meter();
    setState("listening");
  },

  _meter() {
    try {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = this.audioCtx.createMediaStreamSource(this.stream);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 512;
      src.connect(this.analyser);
      const data = new Uint8Array(this.analyser.fftSize);
      const tick = () => {
        if (!this.active || !this.analyser) { app.micLevel = 0; return; }
        this.analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += ((v - 128) / 128) ** 2;
        app.micLevel = Math.min(1, Math.sqrt(sum / data.length) * 4);
        requestAnimationFrame(tick);
      };
      tick();
    } catch (_) { /* level meter is cosmetic */ }
  },

  async stop() {
    if (!this.held) return;
    this.held = false;
    $("ptt").classList.remove("active");
    if (!this.rec) return settle();
    const rec = this.rec;
    const duration = performance.now() - this.startedAt;
    const done = new Promise((r) => (rec.onstop = r));
    rec.stop();
    await done;
    const blob = new Blob(this.chunks, { type: rec.mimeType || "audio/webm" });
    this._release();
    if (duration < 350 || blob.size < 1000) {
      note("That was a bit short — hold the button (or Space) while you speak.");
      return settle();
    }
    await transcribeAndSend(blob);
  },

  _release() {
    this.active = false;
    app.micLevel = 0;
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.audioCtx) this.audioCtx.close().catch(() => {});
    this.stream = this.rec = this.audioCtx = this.analyser = null;
  },
};

async function transcribeAndSend(blob) {
  const ext = blob.type.includes("mp4") ? "mp4" : blob.type.includes("ogg") ? "ogg" : "webm";
  const form = new FormData();
  form.append("file", blob, "speech." + ext);
  app.busy = true;
  setState("transcribing");
  let text = "";
  try {
    const res = await fetch("/oj/v1/speech/transcribe", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(res.status === 501
        ? "OpenJarvis has no speech-to-text backend (faster-whisper) loaded. See README → Microphone."
        : (data.detail || `HTTP ${res.status}`));
    }
    text = (data.text || "").trim();
  } catch (e) {
    app.busy = false;
    note("Transcription failed: " + e.message);
    setState("error");
    return setTimeout(settle, 2500);
  }
  app.busy = false;
  if (!text) {
    note("I didn't catch anything there.");
    return settle();
  }
  await converse(text, "voice");
}

// ---------------------------------------------------------------- briefing
async function briefing() {
  const btn = $("briefing");
  btn.disabled = true;
  await speech.stop();
  app.busy = true;
  setState("thinking", "Preparing briefing");
  note("Generating your morning briefing with the OpenJarvis morning digest… this can take a minute.");
  try {
    const res = await api("/oj/api/digest/generate", { method: "POST", body: "{}" });
    const text = (res && res.text ? stripThink(res.text) : "").trim();
    if (!text) throw new Error("the digest came back empty");
    addEntry("assistant", text, "☀ morning digest");
    app.history.push({ role: "assistant", content: text });
    app.busy = false;
    speech.enqueue(text);
  } catch (e) {
    app.busy = false;
    note("Briefing failed: " + e.message);
    setState("error");
    setTimeout(settle, 2500);
  } finally {
    btn.disabled = false;
    if (!speech.active && app.state !== "error") settle();
  }
}

// ---------------------------------------------------------------- settings & status
async function saveSettings(patch) {
  Object.assign(app.settings, patch);
  try { app.settings = await api("/api/settings", { method: "POST", body: JSON.stringify(patch) }); }
  catch (e) { note("Couldn't save settings: " + e.message); }
}

function fmtSize(bytes) {
  if (!bytes) return "";
  return (bytes / 1e9).toFixed(1) + " GB";
}

async function loadModels() {
  const sel = $("model");
  let data;
  try { data = await api("/api/models"); } catch (e) { note("Couldn't list models: " + e.message); return []; }
  sel.innerHTML = "";
  for (const m of data.models) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = [m.id, m.parameter_size, fmtSize(m.size_bytes)].filter(Boolean).join(" · ");
    sel.appendChild(o);
  }
  if (!data.models.some((m) => m.id === app.settings.model) && app.settings.model) {
    const o = document.createElement("option");
    o.value = app.settings.model;
    o.textContent = app.settings.model + " (not found)";
    sel.prepend(o);
  }
  sel.value = app.settings.model;
  if (data.errors && data.errors.length && !data.models.length) note("Model list: " + data.errors.join("; "));
  app.models = data.models;
  return data.models;
}

async function loadVoices() {
  const sel = $("voice");
  sel.innerHTML = "";
  let data = { engine: null, voices: [] };
  try { data = await api("/api/voices"); } catch (_) { /* fall through */ }
  let voices = data.voices;
  if (data.engine === "macos-say" && voices.length) {
    app.ttsEngine = "macos-say";
  } else if (window.speechSynthesis) {
    app.ttsEngine = "browser";
    voices = speechSynthesis.getVoices().map((v) => ({ name: v.name, lang: v.lang }));
    if (!voices.length) {
      await sleep(400);
      voices = speechSynthesis.getVoices().map((v) => ({ name: v.name, lang: v.lang }));
    }
    note("macOS `say` not found — using the browser's voices instead.");
  } else {
    app.ttsEngine = null;
    note("No text-to-speech available; replies will be text only.");
  }
  // English voices first, then the rest.
  voices.sort((a, b) => (b.lang.startsWith("en") - a.lang.startsWith("en")) || a.name.localeCompare(b.name));
  for (const v of voices) {
    const o = document.createElement("option");
    o.value = v.name;
    o.textContent = `${v.name} (${v.lang})`;
    sel.appendChild(o);
  }
  const preferred = voices.find((v) => v.name === app.settings.voice) ? app.settings.voice
    : data.default || (voices[0] && voices[0].name) || "";
  sel.value = preferred;
  if (preferred && preferred !== app.settings.voice) {
    if (app.settings.voice === "Daniel" && app.ttsEngine === "macos-say") {
      note(`The Daniel voice isn't installed; using ${preferred}. Add Daniel in System Settings → Accessibility → Spoken Content → System Voice → Manage Voices.`);
    }
    app.settings.voice = preferred;
  }
  $("dot-tts").className = "dot " + (app.ttsEngine === "macos-say" ? "ok" : app.ttsEngine ? "warn" : "bad");
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("dot-oj").className = "dot " + (s.openjarvis ? "ok" : "bad");
    $("dot-ollama").className = "dot " + (s.ollama ? "ok" : "bad");
    app.sttAvailable = !!(s.speech_to_text && s.speech_to_text.available);
    $("dot-stt").className = "dot " + (app.sttAvailable ? "ok" : "bad");
    $("dot-stt").title = app.sttAvailable
      ? "Speech-to-text: " + (s.speech_to_text.backend || "ready")
      : "Speech-to-text unavailable: " + ((s.speech_to_text && s.speech_to_text.reason) || "unknown");
    return s;
  } catch (_) {
    for (const id of ["dot-oj", "dot-ollama", "dot-stt"]) $(id).className = "dot bad";
  }
}

// ---------------------------------------------------------------- model comparison
const COMPARE_PROMPTS = [
  { id: "fact", prompt: "In one sentence, why is the sky blue?" },
  { id: "persona", prompt: "I've been debugging the same bug for six hours. Say something to keep me going." },
  {
    id: "reasoning",
    prompt: "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Answer briefly.",
    check: (t) => /(\b5\s*cents?\b|\$?0?\.05\b|five cents)/i.test(t),
  },
];

function openCompare() {
  const box = $("compare-models");
  box.innerHTML = "";
  for (const m of app.models || []) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = m.id;
    cb.checked = true;
    label.append(cb, " " + m.id + (m.parameter_size ? ` (${m.parameter_size})` : ""));
    box.appendChild(label);
  }
  try {
    const saved = JSON.parse(localStorage.getItem("jarvis.compare") || "null");
    if (saved) renderCompare(saved);
  } catch (_) { /* ignore */ }
  $("compare").hidden = false;
}

async function runCompare() {
  const models = [...$("compare-models").querySelectorAll("input:checked")].map((c) => c.value);
  if (!models.length) return;
  await speech.stop();
  $("compare-run").disabled = true;
  const results = { at: new Date().toISOString(), rows: [] };
  try {
    for (const model of models) {
      $("compare-progress").textContent = `Loading ${model}…`;
      let load = null;
      try {
        load = await streamChat([{ role: "user", content: "Reply with the single word: ready" }], model);
      } catch (e) {
        results.rows.push({ model, error: e.message });
        renderCompare(results);
        continue;
      }
      for (const p of COMPARE_PROMPTS) {
        $("compare-progress").textContent = `${model}: ${p.id}…`;
        try {
          const r = await streamChat([{ role: "system", content: PERSONA }, { role: "user", content: p.prompt }], model);
          const genSecs = Math.max(0.001, (r.totalMs - (r.ttftMs || 0)) / 1000);
          results.rows.push({
            model, prompt: p.id, loadMs: load.totalMs,
            ttftMs: r.ttftMs, totalMs: r.totalMs,
            rate: r.chunks > 1 ? (r.chunks - 1) / genSecs : null,
            answer: r.text, correct: p.check ? p.check(r.text) : null,
          });
        } catch (e) {
          results.rows.push({ model, prompt: p.id, error: e.message });
        }
        renderCompare(results);
      }
    }
  } finally {
    $("compare-run").disabled = false;
    $("compare-progress").textContent = "Done.";
    try { localStorage.setItem("jarvis.compare", JSON.stringify(results)); } catch (_) {}
  }
}

function renderCompare(results) {
  const wrap = $("compare-results");
  wrap.innerHTML = "";
  const byModel = {};
  for (const r of results.rows) (byModel[r.model] ||= []).push(r);

  const summary = document.createElement("table");
  summary.className = "results";
  summary.innerHTML = "<tr><th>Model</th><th>Load (1st call)</th><th>Avg first token</th><th>Avg speed</th><th>Avg reply time</th><th>Reasoning check</th><th></th></tr>";
  for (const [model, rows] of Object.entries(byModel)) {
    const ok = rows.filter((r) => !r.error);
    const avg = (k) => ok.length ? ok.reduce((s, r) => s + (r[k] || 0), 0) / ok.length : null;
    const reason = ok.find((r) => r.prompt === "reasoning");
    const tr = document.createElement("tr");
    const cells = [
      model,
      ok[0] ? (ok[0].loadMs / 1000).toFixed(1) + " s" : (rows[0].error || "—"),
      avg("ttftMs") != null ? (avg("ttftMs") / 1000).toFixed(2) + " s" : "—",
      avg("rate") != null ? avg("rate").toFixed(1) + " tok/s" : "—",
      avg("totalMs") != null ? (avg("totalMs") / 1000).toFixed(1) + " s" : "—",
      reason ? (reason.correct ? "✓ correct" : "✗ wrong") : "—",
    ];
    for (const c of cells) { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    const td = document.createElement("td");
    const b = document.createElement("button");
    b.className = "small";
    b.textContent = model === app.settings.model ? "Default ✓" : "Make default";
    b.onclick = async () => { await saveSettings({ model }); $("model").value = model; renderCompare(results); };
    td.appendChild(b);
    tr.appendChild(td);
    summary.appendChild(tr);
  }
  wrap.appendChild(summary);

  const h = document.createElement("p");
  h.className = "hint";
  h.textContent = "Answers — judge tone and quality yourself:";
  wrap.appendChild(h);
  const detail = document.createElement("table");
  detail.className = "results";
  detail.innerHTML = "<tr><th>Model</th><th>Prompt</th><th>First token</th><th>Answer</th></tr>";
  for (const r of results.rows) {
    const tr = document.createElement("tr");
    for (const c of [r.model, r.prompt || "load", r.ttftMs ? (r.ttftMs / 1000).toFixed(2) + " s" : "—", r.error ? "Error: " + r.error : r.answer]) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    tr.lastChild.className = "answer";
    detail.appendChild(tr);
  }
  wrap.appendChild(detail);
}

// ---------------------------------------------------------------- orb
const orb = (() => {
  const canvas = $("orb");
  const ctx = canvas.getContext("2d");
  const TARGETS = {
    idle:       { hue: 205, light: 0.55, scale: 0.92, wobble: 0.025, speed: 0.6, glow: 0.45, spin: 0.0, rings: 0 },
    listening:  { hue: 188, light: 0.75, scale: 1.04, wobble: 0.05,  speed: 1.6, glow: 0.85, spin: 0.0, rings: 1 },
    thinking:   { hue: 228, light: 0.62, scale: 0.86, wobble: 0.04,  speed: 2.8, glow: 0.65, spin: 1.0, rings: 0 },
    speaking:   { hue: 200, light: 0.85, scale: 1.0,  wobble: 0.07,  speed: 2.2, glow: 1.0,  spin: 0.0, rings: 0 },
    error:      { hue: 352, light: 0.55, scale: 0.9,  wobble: 0.03,  speed: 0.9, glow: 0.6,  spin: 0.0, rings: 0 },
  };
  const cur = { ...TARGETS.idle };
  const ripples = [];
  let lastRipple = 0, w = 0, h = 0, dpr = 1;

  function resize() {
    dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    w = r.width; h = r.height;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  window.addEventListener("resize", resize);
  resize();

  function frame(now) {
    const t = now / 1000;
    const visual = app.state === "transcribing" ? "thinking" : app.state;
    const tgt = TARGETS[visual] || TARGETS.idle;
    for (const k in tgt) cur[k] += (tgt[k] - cur[k]) * 0.06;

    // Speaking envelope: syllable-like pulses (macOS speech plays outside the
    // browser, so we animate a plausible rhythm rather than the real waveform).
    const speakEnv = visual === "speaking"
      ? 0.5 + 0.5 * Math.abs(Math.sin(t * 7.3) * Math.sin(t * 2.1 + 1.3)) : 0;
    const level = visual === "listening" ? app.micLevel : speakEnv * 0.8;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const R = Math.min(w, h) * 0.22 * cur.scale * (1 + level * 0.12 + Math.sin(t * cur.speed) * 0.015);
    const hue = cur.hue;

    // outer glow
    const glow = ctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, R * 2.6);
    glow.addColorStop(0, `hsla(${hue}, 100%, 60%, ${0.35 * cur.glow})`);
    glow.addColorStop(0.45, `hsla(${hue}, 100%, 50%, ${0.10 * cur.glow})`);
    glow.addColorStop(1, "hsla(210, 100%, 40%, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);

    ctx.globalCompositeOperation = "lighter";

    // listening ripples
    if (cur.rings > 0.5 && now - lastRipple > 420 - level * 250) {
      ripples.push({ born: now, strength: 0.4 + level });
      lastRipple = now;
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      const age = (now - ripples[i].born) / 1600;
      if (age > 1) { ripples.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.arc(cx, cy, R * (1.05 + age * 1.1), 0, Math.PI * 2);
      ctx.strokeStyle = `hsla(${hue}, 100%, 70%, ${(1 - age) * 0.5 * ripples[i].strength})`;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    // layered plasma blob
    for (let layer = 0; layer < 3; layer++) {
      const amp = cur.wobble * (1 + layer * 0.6) * (1 + level * 2.2);
      const sp = cur.speed * (1 + layer * 0.35);
      ctx.beginPath();
      const N = 120;
      for (let i = 0; i <= N; i++) {
        const a = (i / N) * Math.PI * 2;
        const n =
          Math.sin(a * 3 + t * sp + layer) * 0.5 +
          Math.sin(a * 5 - t * sp * 1.3 + layer * 2) * 0.3 +
          Math.sin(a * 8 + t * sp * 0.7) * 0.2;
        const r = R * (1 - layer * 0.12) * (1 + amp * n);
        const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      const g = ctx.createRadialGradient(cx - R * 0.25, cy - R * 0.3, R * 0.05, cx, cy, R * 1.1);
      g.addColorStop(0, `hsla(${hue - 15}, 100%, ${70 + cur.light * 25}%, ${0.55 * cur.light})`);
      g.addColorStop(0.5, `hsla(${hue}, 100%, 55%, ${0.32 * cur.light})`);
      g.addColorStop(1, `hsla(${hue + 15}, 100%, 40%, 0.02)`);
      ctx.fillStyle = g;
      ctx.fill();
      ctx.strokeStyle = `hsla(${hue - 10}, 100%, 75%, ${0.25 * cur.light})`;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }

    // thinking: orbiting particles and arcs
    if (cur.spin > 0.05) {
      for (let i = 0; i < 3; i++) {
        const start = t * (1.6 + i * 0.7) * (i % 2 ? -1 : 1) + i * 2;
        ctx.beginPath();
        ctx.arc(cx, cy, R * (1.25 + i * 0.12), start, start + 1.2 + i * 0.3);
        ctx.strokeStyle = `hsla(${hue + i * 12}, 100%, 72%, ${0.55 * cur.spin})`;
        ctx.lineWidth = 2.5 - i * 0.6;
        ctx.stroke();
      }
      for (let i = 0; i < 14; i++) {
        const a = t * 1.8 + (i / 14) * Math.PI * 2;
        const rr = R * (1.35 + 0.08 * Math.sin(t * 3 + i));
        const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr * 0.42;
        ctx.beginPath();
        ctx.arc(x, y, 2.2, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${hue}, 100%, 80%, ${0.8 * cur.spin})`;
        ctx.fill();
      }
    }

    // speaking: concentric voice bars around the orb
    if (visual === "speaking" || cur.light > 0.8) {
      const bars = 64;
      for (let i = 0; i < bars; i++) {
        const a = (i / bars) * Math.PI * 2;
        const mag = speakEnv * (0.4 + 0.6 * Math.abs(Math.sin(i * 1.7 + t * 9)));
        const r1 = R * 1.12, r2 = r1 + R * 0.28 * mag;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
        ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2);
        ctx.strokeStyle = `hsla(${hue}, 100%, 70%, ${0.6 * mag})`;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    // bright core
    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.45);
    core.addColorStop(0, `hsla(${hue - 20}, 100%, 95%, ${0.55 + level * 0.4})`);
    core.addColorStop(1, `hsla(${hue}, 100%, 60%, 0)`);
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.45, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalCompositeOperation = "source-over";
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return { resize };
})();

// ---------------------------------------------------------------- wiring
function isTypingTarget(el) {
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
}

function wire() {
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("input").value;
    $("input").value = "";
    converse(text, "typed");
  });

  const ptt = $("ptt");
  ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); ptt.setPointerCapture(e.pointerId); recorder.start(); });
  ptt.addEventListener("pointerup", () => recorder.stop());
  ptt.addEventListener("pointercancel", () => recorder.stop());

  window.addEventListener("keydown", (e) => {
    if (e.code === "Escape") { stopAll(); return; }
    if (isTypingTarget(document.activeElement)) return;
    if (e.code === "Space" && !e.repeat) { e.preventDefault(); recorder.start(); }
    if (e.key === "f" || e.key === "F") toggleFullscreen();
  });
  window.addEventListener("keyup", (e) => {
    if (e.code === "Space" && recorder.held) { e.preventDefault(); recorder.stop(); }
  });
  window.addEventListener("blur", () => recorder.held && recorder.stop());

  $("stop").addEventListener("click", stopAll);
  $("mute").addEventListener("click", async () => {
    const muted = !app.settings.muted;
    if (muted) await speech.stop();
    await saveSettings({ muted });
    renderMute();
    settle();
  });
  $("model").addEventListener("change", (e) => saveSettings({ model: e.target.value }));
  $("voice").addEventListener("change", async (e) => {
    await saveSettings({ voice: e.target.value });
    speech.enqueue("Voice changed. How do I sound?");
  });
  $("clear").addEventListener("click", () => { app.history = []; $("transcript").innerHTML = ""; });
  $("briefing").addEventListener("click", briefing);
  $("fullscreen").addEventListener("click", toggleFullscreen);
  $("compare-open").addEventListener("click", openCompare);
  $("compare-close").addEventListener("click", () => ($("compare").hidden = true));
  $("compare-run").addEventListener("click", runCompare);
}

async function stopAll() {
  await speech.stop();
  if (app.chatAbort) app.chatAbort.abort();
  settle();
}

function renderMute() {
  const b = $("mute");
  b.setAttribute("aria-pressed", String(!!app.settings.muted));
  b.textContent = app.settings.muted ? "🔇 Muted" : "🔊 Voice on";
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

async function init() {
  wire();
  try { app.settings = await api("/api/settings"); } catch (_) { /* defaults */ }
  renderMute();
  await Promise.all([loadModels(), loadVoices(), refreshStatus()]);
  setInterval(refreshStatus, 10000);
  setState("idle");
  app.ready = true;
}

init();
