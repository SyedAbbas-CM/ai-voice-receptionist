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

// Streaming TTS: fetch /voice/tts-stream (NDJSON), decode each chunk as it
// arrives, queue and play sequentially. First audio arrives ~1.5-2s instead
// of waiting for the whole reply to synth (was 5-10s+).
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

    // Play chunks strictly in seq order — but decode/queue as they arrive.
    // Each chunk plays on an <audio> element; the promise resolves at 'ended'
    // so the NEXT queued chunk starts immediately.
    const playQueue = [];
    let playing = false;

    const drainQueue = async () => {
      if (playing) return;
      playing = true;
      while (playQueue.length > 0) {
        const src = playQueue.shift();
        const audio = new Audio(src);
        await new Promise((res, rej) => {
          audio.onended = res;
          audio.onerror = res;   // don't stall on a bad chunk
          audio.play().catch(res);
        });
      }
      playing = false;
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // Split on newline; last partial line stays in buf
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch { continue; }
        if (obj.seq === -1) {
          // Sentinel — end of stream or error
          if (obj.error) console.warn("tts stream error:", obj.error);
          continue;
        }
        if (obj.provider === "browser") {
          browserSpeak(obj.speak || text);
          continue;
        }
        if (obj.audio_b64) {
          playQueue.push(`data:${obj.mime};base64,${obj.audio_b64}`);
          drainQueue();  // fire-and-forget; internal lock prevents overlap
        }
      }
    }
    // Wait for the final chunk to finish playing before returning
    while (playing || playQueue.length > 0) {
      await new Promise(res => setTimeout(res, 100));
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
