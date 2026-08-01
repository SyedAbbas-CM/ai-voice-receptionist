const API = (() => {
  // When served from FastAPI (mounted at /), use same origin. Otherwise fall back to localhost:8000.
  const sameOrigin = window.location.protocol.startsWith("http") && window.location.port !== "";
  return sameOrigin ? "" : "http://localhost:8000";
})();

const els = {
  config: document.getElementById("config"),
  status: document.getElementById("status"),
  startBtn: document.getElementById("start-btn"),
  recordBtn: document.getElementById("record-btn"),
  endBtn: document.getElementById("end-btn"),
  textToggle: document.getElementById("text-toggle"),
  textWrap: document.getElementById("text-input-wrap"),
  textInput: document.getElementById("text-input"),
  textSend: document.getElementById("text-send"),
  transcript: document.getElementById("transcript"),
  extracted: document.getElementById("extracted"),
  tools: document.getElementById("tools"),
  meta: document.getElementById("meta"),
};

let sessionId = null;
let textMode = false;
let mediaRecorder = null;
let recordedChunks = [];

function setStatus(s) { els.status.textContent = s; }

function addTurn(role, text) {
  const div = document.createElement("div");
  div.className = `turn ${role}`;
  div.innerHTML = `<div class="role">${role}</div><div>${escapeHtml(text)}</div>`;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadConfig() {
  try {
    const r = await fetch(`${API}/config`);
    const c = await r.json();
    els.config.textContent = `llm=${c.llm} · stt=${c.stt} · tts=${c.tts}`;
  } catch {
    els.config.textContent = "(backend unreachable)";
  }
}

async function startCall() {
  setStatus("starting...");
  const r = await fetch(`${API}/chat/start`, { method: "POST" });
  if (!r.ok) { setStatus(`error: ${r.status}`); return; }
  const data = await r.json();
  sessionId = data.session_id;
  els.transcript.innerHTML = "";
  addTurn("assistant", data.greeting);
  await speak(data.greeting);
  els.startBtn.disabled = true;
  els.recordBtn.disabled = false;
  els.endBtn.disabled = false;
  setStatus(`live · ${sessionId}`);
}

async function endCall() {
  if (!sessionId) return;
  await fetch(`${API}/chat/end`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, text: "" }),
  });
  setStatus(`ended · ${sessionId}`);
  sessionId = null;
  els.startBtn.disabled = false;
  els.recordBtn.disabled = true;
  els.endBtn.disabled = true;
}

async function sendUserText(text) {
  if (!sessionId || !text.trim()) return;
  addTurn("user", text);
  setStatus("thinking...");
  const r = await fetch(`${API}/chat/turn`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, text }),
  });
  if (!r.ok) { setStatus(`error: ${r.status}`); return; }
  const data = await r.json();
  addTurn("assistant", data.reply);
  els.extracted.textContent = JSON.stringify(data.extracted, null, 2);
  els.tools.textContent = JSON.stringify(data.tool_results, null, 2);
  els.meta.textContent = JSON.stringify({ status: data.status, escalated: data.escalated }, null, 2);
  if (data.tool_results) {
    data.tool_results.forEach(tc => addTurn("tool", `${tc.name}(${JSON.stringify(tc.arguments)}) -> ${JSON.stringify(tc.result)}`));
  }
  setStatus(data.escalated ? "escalated" : "live");
  await speak(data.reply);
}

// Reusable AudioContext for gap-free sample-accurate chunk playback.
// Created lazily on first speak() so autoplay policy is satisfied by the
// user gesture (Start Call button).
let _audioCtx = null;
function _getAudioCtx() {
  if (_audioCtx && _audioCtx.state !== "closed") return _audioCtx;
  _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}

// Decode a base64 WAV/audio blob → AudioBuffer we can schedule.
// Cartesia + our TTS providers stream RAW PCM s16le (no WAV header).  Browser
// decodeAudioData needs a container, so for raw PCM we build the AudioBuffer
// by hand from Int16 samples.  Fixed 2026-07-31 (was silently dropping audio).
async function _decodeChunk(b64, mime) {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const ctx = _getAudioCtx();
  if (mime && mime.startsWith("audio/pcm")) {
    const rateMatch = mime.match(/rate=(\d+)/);
    const sampleRate = rateMatch ? parseInt(rateMatch[1], 10) : 16000;
    const dv = new DataView(bytes.buffer);
    const nSamples = Math.floor(bytes.length / 2);
    const buf = ctx.createBuffer(1, nSamples, sampleRate);
    const chan = buf.getChannelData(0);
    for (let i = 0; i < nSamples; i++) {
      chan[i] = dv.getInt16(i * 2, true) / 32768;
    }
    return buf;
  }
  return await ctx.decodeAudioData(bytes.buffer);
}

// Streaming TTS: fetch /voice/tts-stream (NDJSON) and schedule each chunk
// back-to-back via Web Audio API. Zero gap between chunks (unlike <audio>
// element chain which had ~100-300ms load overhead per chunk). First audio
// arrives ~1.5-2s; subsequent chunks slot in with sample-accurate timing.
async function speak(text) {
  try {
    const r = await fetch(`${API}/voice/tts-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error(`tts-stream ${r.status}`);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const ctx = _getAudioCtx();
    if (ctx.state === "suspended") await ctx.resume();

    // Scheduling cursor — when the NEXT chunk should start playing.
    // Initialized when the first chunk decodes.
    let nextStartAt = 0;
    const sources = [];  // keep refs so GC doesn't kill mid-playback

    const scheduleChunk = async (b64, mime) => {
      try {
        const audioBuf = await _decodeChunk(b64, mime);
        const src = ctx.createBufferSource();
        src.buffer = audioBuf;
        src.connect(ctx.destination);
        // First chunk starts ~50ms in the future to give decoder margin
        const startAt = Math.max(ctx.currentTime + 0.05, nextStartAt);
        src.start(startAt);
        nextStartAt = startAt + audioBuf.duration;
        sources.push(src);
      } catch (e) {
        console.warn("chunk decode/schedule failed:", e);
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch { continue; }
        if (obj.seq === -1) {
          if (obj.error) console.warn("tts stream error:", obj.error);
          continue;
        }
        if (obj.provider === "browser") {
          browserSpeak(obj.speak || text);
          continue;
        }
        if (obj.audio_b64) {
          // Schedule this chunk — no await inside the loop, we WANT
          // multiple decodes racing so subsequent chunks are ready before
          // their scheduled start time.
          scheduleChunk(obj.audio_b64, obj.mime);
        }
      }
    }
    // Wait for the last scheduled chunk to finish playing before returning
    if (nextStartAt > ctx.currentTime) {
      const remainingMs = (nextStartAt - ctx.currentTime) * 1000;
      await new Promise(res => setTimeout(res, remainingMs + 100));
    }
  } catch (e) {
    console.warn("tts-stream failed, falling back:", e);
    browserSpeak(text);
  }
}

function browserSpeak(text) {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05;
  u.pitch = 1.0;
  window.speechSynthesis.speak(u);
}

// Minimum recording duration. Whisper hallucinates ("Oh my god!", "Thank you.")
// on anything shorter than ~0.7s. Enforce here so we don't POST short clips.
const MIN_RECORDING_MS = 700;
let recordingStartedAt = 0;

async function startRecording() {
  if (!navigator.mediaDevices) { setStatus("no mic"); return; }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      sampleRate: 16000,
    },
  });
  recordedChunks = [];
  // Prefer WAV/PCM if the browser supports it (Groq handles it best).
  // Chrome/Firefox don't — fall back to WebM/Opus (works, but codec suffix must
  // be stripped server-side before hitting Groq).
  let mime = "audio/webm;codecs=opus";
  if (MediaRecorder.isTypeSupported("audio/wav")) mime = "audio/wav";
  else if (MediaRecorder.isTypeSupported("audio/ogg;codecs=opus")) mime = "audio/ogg;codecs=opus";
  else if (!MediaRecorder.isTypeSupported(mime)) mime = "audio/webm";

  mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
  mediaRecorder.ondataavailable = e => { if (e.data.size > 0) recordedChunks.push(e.data); };
  mediaRecorder.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    const elapsed = Date.now() - recordingStartedAt;
    const blob = new Blob(recordedChunks, { type: mime });

    if (elapsed < MIN_RECORDING_MS) {
      setStatus(`too short (${elapsed}ms) - hold longer`);
      return;
    }
    if (blob.size < 2000) {
      setStatus("too short - hold longer");
      return;
    }

    setStatus("transcribing...");
    const form = new FormData();
    const ext = mime.includes("wav") ? "wav"
              : mime.includes("ogg") ? "ogg"
              : "webm";
    form.append("audio", blob, `audio.${ext}`);
    form.append("mime", mime);
    const r = await fetch(`${API}/voice/stt`, { method: "POST", body: form });
    if (!r.ok) { setStatus(`stt error ${r.status}`); return; }
    const { transcript } = await r.json();
    if (!transcript.trim()) { setStatus("no speech detected - try again"); return; }
    await sendUserText(transcript);
  };
  mediaRecorder.start(100);  // flush every 100ms so we don't lose the last chunk
  recordingStartedAt = Date.now();
  els.recordBtn.classList.add("recording");
  setStatus("recording... (hold at least 1 second)");
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    els.recordBtn.classList.remove("recording");
  }
}

function setupRecordButton() {
  const btn = els.recordBtn;
  btn.addEventListener("mousedown", startRecording);
  btn.addEventListener("mouseup", stopRecording);
  btn.addEventListener("mouseleave", stopRecording);
  btn.addEventListener("touchstart", e => { e.preventDefault(); startRecording(); });
  btn.addEventListener("touchend", e => { e.preventDefault(); stopRecording(); });
}

els.startBtn.addEventListener("click", startCall);
els.endBtn.addEventListener("click", endCall);
els.textToggle.addEventListener("click", () => {
  textMode = !textMode;
  els.textWrap.classList.toggle("hidden", !textMode);
  els.textToggle.textContent = textMode ? "Use voice mode" : "Use text mode";
});
els.textSend.addEventListener("click", () => {
  const v = els.textInput.value;
  els.textInput.value = "";
  sendUserText(v);
});
els.textInput.addEventListener("keydown", e => {
  if (e.key === "Enter") { els.textSend.click(); }
});

setupRecordButton();
loadConfig();
