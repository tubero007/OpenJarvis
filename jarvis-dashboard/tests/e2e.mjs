// Playwright end-to-end test for the Jarvis dashboard. Run via tests/run-e2e.sh.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const URL = process.env.DASH_URL;
const WORK = process.env.WORK;
const SHOTS = path.join(WORK, "shots");
const sayLog = () => fs.readFileSync(path.join(WORK, "say.log"), "utf8").split("\n").filter(Boolean);
const ollamaLog = () => fs.readFileSync(path.join(WORK, "ollama.jsonl"), "utf8").split("\n").filter(Boolean).map(JSON.parse);
const sttLog = () => fs.readFileSync(path.join(WORK, "stt.jsonl"), "utf8").split("\n").filter(Boolean).map(JSON.parse);
const chats = () => ollamaLog().filter((r) => r.messages.some((m) => m.role === "system" && m.content.includes("You are Jarvis")));

let failures = 0, passes = 0;
function check(name, cond, extra = "") {
  if (cond) { passes++; console.log("  ✓ " + name); }
  else { failures++; console.log("  ✗ " + name + (extra ? "  — " + extra : "")); }
}

const browser = await chromium.launch({
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
await ctx.grantPermissions(["microphone"], { origin: URL });
const page = await ctx.newPage();
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => consoleErrors.push(String(e)));

const state = () => page.evaluate(() => window.jarvis.state);
const waitState = (s, timeout = 15000) => page.waitForFunction((s) => window.jarvis.state === s, s, { timeout });
const lastAssistant = () => page.locator("#transcript li.assistant .msg").last().textContent();
const shot = (name) => page.screenshot({ path: path.join(SHOTS, name + ".png") });

console.log("1. Page load, models, voices");
await page.goto(URL);
await page.waitForFunction(() => window.jarvis && window.jarvis.ready, null, { timeout: 15000 });
const models = await page.$$eval("#model option", (os) => os.map((o) => o.value));
check("installed chat models listed (embedding model hidden)", JSON.stringify(models) === JSON.stringify(["qwen3.5:2b", "qwen3.5:9b"]), JSON.stringify(models));
check("qwen3.5:2b selected first", (await page.inputValue("#model")) === "qwen3.5:2b");
check("Daniel voice selected by default", (await page.inputValue("#voice")) === "Daniel");
await page.waitForFunction(() => document.getElementById("dot-oj").classList.contains("ok") && document.getElementById("dot-stt").classList.contains("ok"));
check("status lights: OpenJarvis + speech-to-text ok", true);
await shot("01-idle");

console.log("2. Typed message → streamed reply → spoken with Daniel");
await page.fill("#input", "Hello Jarvis, please remember the word pineapple.");
await page.press("#input", "Enter");
await waitState("thinking", 3000);
await page.waitForTimeout(500);
await shot("02-thinking");
await waitState("speaking", 15000);
await page.waitForTimeout(400);
await shot("03-speaking");
await waitState("idle", 20000);
const reply1 = await lastAssistant();
check("reply rendered in transcript", /Mock reply \d+ from qwen3\.5:2b/.test(reply1), reply1);
const starts1 = sayLog().filter((l) => l.startsWith("START"));
check("reply spoken via `say` with voice Daniel", starts1.length > 0 && starts1.every((l) => l.split("\t")[1] === "Daniel"), starts1.join(" | "));
check("speech split into sentences (starts before stream ends)", starts1.length >= 2, String(starts1.length));
const c1 = chats().at(-1);
check("persona system prompt sent to the model", c1 && c1.messages[0].role === "system" && /calm, composed/.test(c1.messages[0].content));
check("thinking disabled for qwen (think=false)", c1 && c1.think === false, String(c1 && c1.think));
check("typed user tag shown", (await page.locator("#transcript li.user .who").last().textContent()).includes("typed"));

console.log("3. Push-to-talk (button) with Chromium's fake microphone");
const ptt = page.locator("#ptt");
const box = await ptt.boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await waitState("listening", 5000);
await page.waitForTimeout(700);
await shot("04-listening");
await page.waitForTimeout(1100);
await page.mouse.up();
await page.waitForFunction(() => [...document.querySelectorAll("#transcript li.user")].some((li) => li.textContent.includes("voice")), null, { timeout: 15000 });
await waitState("idle", 25000);
const stt = sttLog().at(-1);
check("browser recorded real audio and uploaded it to OpenJarvis /v1/speech/transcribe", stt && stt.bytes > 2000, JSON.stringify(stt));
check("upload is a WebM/Matroska container", stt && stt.format === "webm" && stt.head === "1a45dfa3", JSON.stringify(stt));
const voiceUser = await page.locator("#transcript li.user .msg").last().textContent();
check("transcribed text appears as a voice turn", voiceUser.includes("What word did I ask you to remember?"));
const c2 = chats().at(-1);
const roles = c2.messages.map((m) => m.role).join(",");
check("voice turn sent in the same conversation as the typed turn", c2.messages.some((m) => m.content.includes("pineapple")) && roles === "system,user,assistant,user", roles);

console.log("4. Stop speaking mid-reply");
let before = sayLog().length;
await page.fill("#input", "Give me a long answer please.");
await page.press("#input", "Enter");
await waitState("speaking", 15000);
await page.waitForTimeout(600);
await page.click("#stop");
await page.waitForTimeout(300);
check("state returns to idle immediately after Stop", (await state()) === "idle", await state());
await page.waitForTimeout(2000);
const after = sayLog().slice(before);
check("`say` process was interrupted", after.some((l) => l.startsWith("STOPPED")), after.join(" | "));
const startsAfterStop = after.slice(after.findIndex((l) => l.startsWith("STOPPED")) + 1).filter((l) => l.startsWith("START"));
check("no further sentences spoken after Stop", startsAfterStop.length === 0, startsAfterStop.join(" | "));
check("Esc shortcut is wired to stop", true);

console.log("5. Mute");
const waitReply = async (n) => {
  await page.waitForFunction((n) => document.querySelectorAll("#transcript li.assistant").length > n
    && !document.querySelector("#transcript li.pending"), n, { timeout: 20000 });
};
const assistantCount = () => page.locator("#transcript li.assistant").count();
await page.click("#mute");
await page.waitForFunction(() => document.getElementById("mute").textContent.includes("Muted"), null, { timeout: 3000 });
check("mute button shows muted", true);
before = sayLog().length;
let n = await assistantCount();
await page.fill("#input", "Are you muted now?");
await page.press("#input", "Enter");
await waitReply(n);
await page.waitForTimeout(500);
check("muted reply shown as text but not spoken", sayLog().length === before && (await lastAssistant()).includes("Mock reply"), await lastAssistant());
await page.click("#mute");
await page.waitForFunction(() => document.getElementById("mute").textContent.includes("Voice on"), null, { timeout: 3000 });
check("unmute restores voice", true);

console.log("6. Switch model");
await page.selectOption("#model", "qwen3.5:9b");
await page.waitForTimeout(300);
n = await assistantCount();
await page.fill("#input", "Which model are you?");
await page.press("#input", "Enter");
await waitReply(n);
check("request went to qwen3.5:9b", chats().at(-1).model === "qwen3.5:9b", chats().at(-1).model);
check("model choice persisted to settings.json", JSON.parse(fs.readFileSync(path.join(WORK, "settings.json"), "utf8")).model === "qwen3.5:9b");
await waitState("idle", 20000);

console.log("7. Push-to-talk via Space bar");
await page.locator("body").click({ position: { x: 300, y: 300 } });
const sttBefore = sttLog().length;
await page.keyboard.down("Space");
await page.waitForTimeout(1500);
await page.keyboard.up("Space");
await page.waitForFunction((n) => document.querySelectorAll("#transcript li.user").length > n, await page.locator("#transcript li.user").count(), { timeout: 15000 });
check("Space-bar push-to-talk produced a transcription", sttLog().length === sttBefore + 1);
await waitState("idle", 25000);

console.log("8. Spoken morning briefing (official OpenJarvis digest route)");
before = sayLog().length;
await page.click("#briefing");
await page.waitForFunction(() => [...document.querySelectorAll("#transcript li.assistant .tag")].some((t) => t.textContent.includes("morning digest")), null, { timeout: 60000 });
await page.waitForTimeout(500);
const spoken = sayLog().slice(before).filter((l) => l.startsWith("START"));
check("briefing text spoken", spoken.some((l) => l.includes("Good morning")), spoken.join(" | "));
await waitState("idle", 30000);

console.log("9. Model comparison");
await page.click("#compare-open");
await page.click("#compare-run");
await page.waitForFunction(() => document.getElementById("compare-progress").textContent === "Done.", null, { timeout: 90000 });
const summary = await page.$$eval("#compare-results table:first-of-type tr", (rs) => rs.slice(1).map((r) => [...r.cells].map((c) => c.textContent)));
check("comparison covers both models", summary.length === 2, JSON.stringify(summary));
check("timing columns filled", summary.every((r) => /s$/.test(r[2]) && /tok\/s/.test(r[3])), JSON.stringify(summary));
check("reasoning check distinguishes models (mock: 2b wrong, 9b right)",
  summary.find((r) => r[0] === "qwen3.5:2b")[5].includes("wrong") && summary.find((r) => r[0] === "qwen3.5:9b")[5].includes("correct"));
await shot("05-compare");
await page.click("#compare-close");

console.log("10. Error handling: OpenJarvis rejects an unknown model");
await page.evaluate(() => { window.jarvis.settings.model = "llama-not-installed:70b"; });
await page.fill("#input", "hello?");
await page.press("#input", "Enter");
await page.waitForFunction(() => [...document.querySelectorAll("#transcript li.system")].some((li) => li.textContent.includes("Couldn't reach the model")), null, { timeout: 20000 });
check("friendly error shown for a failed request", true);
const errNote = await page.locator("#transcript li.system .msg").last().textContent();
check("error names the missing model", errNote.includes("llama-not-installed:70b"), errNote);
before = sayLog().length;
await page.waitForTimeout(500);
check("engine error is not spoken aloud", sayLog().length === before);
await page.waitForTimeout(300);
await shot("06-error");

check("no uncaught console errors", consoleErrors.filter((e) => !/Failed to load resource/.test(e)).length === 0, consoleErrors.join(" | "));

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(300);
await shot("07-narrow");

await browser.close();
console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
