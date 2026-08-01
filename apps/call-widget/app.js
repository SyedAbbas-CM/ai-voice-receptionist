// Customer-facing call widget. Same backend as /simulator but a clean
// customer UX — no dev panels, no transcript-of-tool-calls, just chat bubbles
// and a big "hold to talk" button.
//
// Reuses:
//   POST /chat/start         → session_id + greeting
//   POST /chat/turn          → reply for a user text
//   POST /chat/end           → close session
//   POST /voice/stt          → mic blob → transcript
//   POST /voice/tts-stream   → text → streaming audio chunks (NDJSON)

const API = (() => {
  const sameOrigin = window.location.protocol.startsWith("http") && window.location.host !== "";
  return sameOrigin ? "" : "http://localhost:8000";
})();

const $ = id => document.getElementById(id);
const els = {
  businessName: $("business-name"),
  callBtn: $("call-btn"),
  screenIdle: $("screen-idle"),
  screenActive: $("screen-active"),
  screenEnded: $("screen-ended"),
  callBusiness: $("call-business"),
  callStatus: $("call-status"),
  callTimer: $("call-timer"),
  pulseRing: $("pulse-ring"),
  transcript: $("transcript-live"),
  talkBtn: $("talk-btn"),
  talkLabel: $("talk-label"),
  endBtn: $("end-btn"),
  restartBtn: $("restart-btn"),
  endedDuration: $("ended-duration"),
  endedTurns: $("ended-turns"),
};

// state
let sessionId = null;
let businessName = "Loading…";
let callStartedAt = 0;
let turnCount = 0;
let timerHandle = null;
let mediaRecorder = null;
let recordedChunks = [];
let recordingStartedAt = 0;
let audioCtx = null;

// ---------- screens ----------

function showScreen(which) {
  ["screenIdle", "screenActive", "screenEnded"].forEach(k => els[k].classList.remove("active"));
  els[which].classList.add("active");
}

function setCallStatus(text, cls = "") {
  els.callStatus.textContent = text;
  els.callStatus.className = "call-status " + cls;
}

function addBubble(who, text) {
  // Clear the placeholder hint on first bubble
  const hint = els.transcript.querySelector(".hint");
  if (hint) hint.remove();
  const wrap = document.createElement("div");
  wrap.className = `turn ${who === "user" ? "you" : "agent"}`;
  const label = who === "user" ? "you" : businessName;
  wrap.innerHTML = `<div class="who">${escape(label)}</div><div class="bubble">${escape(text)}</div>`;
  els.transcript.appendChild(wrap);
  els.transcript.scrollTop = els.transcript.scrollHeight;
  turnCount++;
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- config ----------

async function loadBusinessName() {
  try {
    const r = await fetch(`${API}/config`);
    if (r.ok) {
      // /config doesn't return business name directly; we'll get it from /chat/start's greeting
      // or from the greeting text below. For now stub with a reasonable default.
    }
  } catch {}
  // Try /health first to make sure backend is up
  try {
    const r = await fetch(`${API}/health`);
    if (r.ok) {
      els.businessName.textContent = "Our receptionist";
      els.callBusiness.textContent = "";  // will be updated on /chat/start
    }
  } catch {
    els.businessName.textContent = "(offline)";
    els.callBtn.disabled = true;
  }
}

// ---------- call lifecycle ----------

async function startCall() {
  els.callBtn.disabled = true;
  els.callBtn.textContent = "Connecting…";
  try {
    const r = await fetch(`${API}/chat/start`, { method: "POST" });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    sessionId = data.session_id;
    // Extract business name from the greeting  ("Hi, thanks for calling BUSINESS. ...")
    const match = data.greeting.match(/calling (.+?)\./);
    if (match) {
      businessName = match[1].trim();
      els.businessName.textContent = businessName;
      els.callBusiness.textContent = businessName;
    } else {
      els.callBusiness.textContent = "Our receptionist";
    }
    // switch screens
    showScreen("screenActive");
    callStartedAt = Date.now();
    turnCount = 0;
    startTimer();
    setCallStatus("connected", "listening");
    addBubble("agent", data.greeting);
    await speak(data.greeting);
    setCallStatus("your turn — hold to talk", "listening");
  } catch (e) {
    els.callBtn.textContent = "Call agent";
    els.callBtn.disabled = false;
    alert("Couldn't reach the receptionist. Check that the backend is running.\n" + e);
  }
}

async function endCall(silent = false) {
  if (!sessionId) return;
  stopTimer();
  els.pulseRing.classList.add("hidden");
  try {
    await fetch(`${API}/chat/end`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text: "" }),
    });
  } catch {}
  if (!silent) {
    const duration = Math.round((Date.now() - callStartedAt) / 1000);
    els.endedDuration.textContent = formatDuration(duration);
    els.endedTurns.textContent = turnCount;
    showScreen("screenEnded");
  }
  sessionId = null;
  els.callBtn.textContent = "Call agent";
  els.callBtn.disabled = false;
}

function restartCall() {
  els.transcript.innerHTML = '<div class="hint">Say something. Hold the button below to talk.</div>';
  showScreen("screenIdle");
}

// ---------- timer ----------

function startTimer() {
  stopTimer();
  const tick = () => {
    const s = Math.floor((Date.now() - callStartedAt) / 1000);
    els.callTimer.textContent = formatDuration(s);
  };
  tick();
  timerHandle = setInterval(tick, 1000);
}
function stopTimer() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = null;
}
function formatDuration(s) {
  const m = Math.floor(s / 60);
  const ss = String(s % 60).padStart(2, "0");
  return `${String(m).padStart(2, "0")}:${ss}`;
}

// ---------- brain turn ----------

async function sendUserText(text) {
  if (!sessionId || !text.trim()) return;
  addBubble("user", text);
  setCallStatus("thinking…", "speaking");
  try {
    const r = await fetch(`${API}/chat/turn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    addBubble("agent", data.reply);
    setCallStatus("speaking…", "speaking");
    await speak(data.reply);
    setCallStatus("your turn — hold to talk", "listening");
    if (data.escalated) {
      addBubble("agent", "(a human will follow up)");
    }
  } catch (e) {
    setCallStatus("error — try again", "");
  }
}

// ---------- TTS streaming (copied from simulator) ----------

function getAudioCtx() {
  if (audioCtx && audioCtx.state !== "closed") return audioCtx;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}

async function decodeChunk(b64, mime) {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const ctx = getAudioCtx();
  // Cartesia + our TTS providers stream RAW PCM s16le (no WAV header).
  // decodeAudioData needs a container, so for PCM we build the AudioBuffer
  // by hand from Int16 samples.  Detect via MIME prefix; anything else
  // (mp3/wav/ogg) goes through decodeAudioData.
  if (mime && mime.startsWith("audio/pcm")) {
    // Sample rate is in the MIME like "audio/pcm;rate=16000"
    const rateMatch = mime.match(/rate=(\d+)/);
    const sampleRate = rateMatch ? parseInt(rateMatch[1], 10) : 16000;
    // Interpret bytes as little-endian signed 16-bit samples
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

async function speak(text) {
  try {
    const r = await fetch(`${API}/voice/tts-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error(r.status);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const ctx = getAudioCtx();
    if (ctx.state === "suspended") await ctx.resume();
    let nextStartAt = 0;
    const sources = [];
    const scheduleChunk = async (b64, mime) => {
      try {
        const audioBuf = await decodeChunk(b64, mime);
        const src = ctx.createBufferSource();
        src.buffer = audioBuf;
        src.connect(ctx.destination);
        const startAt = Math.max(ctx.currentTime + 0.05, nextStartAt);
        src.start(startAt);
        nextStartAt = startAt + audioBuf.duration;
        sources.push(src);
      } catch (e) { console.warn("chunk decode failed:", e, "mime=", mime); }
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
        if (obj.seq === -1) continue;
        if (obj.provider === "browser") { browserSpeak(obj.speak || text); continue; }
        if (obj.audio_b64) scheduleChunk(obj.audio_b64, obj.mime);
      }
    }
    if (nextStartAt > ctx.currentTime) {
      await new Promise(res => setTimeout(res, (nextStartAt - ctx.currentTime) * 1000 + 100));
    }
  } catch (e) {
    console.warn("tts stream failed, falling back:", e);
    browserSpeak(text);
  }
}

function browserSpeak(text) {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  window.speechSynthesis.speak(u);
}

// ---------- mic recording ----------

const MIN_RECORDING_MS = 700;

async function startRecording() {
  if (!navigator.mediaDevices) { setCallStatus("no mic access"); return; }
  try {
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
      if (elapsed < MIN_RECORDING_MS || blob.size < 2000) {
        setCallStatus("too short — hold longer", "");
        return;
      }
      setCallStatus("transcribing…", "speaking");
      const form = new FormData();
      const ext = mime.includes("wav") ? "wav" : mime.includes("ogg") ? "ogg" : "webm";
      form.append("audio", blob, `audio.${ext}`);
      form.append("mime", mime);
      try {
        const r = await fetch(`${API}/voice/stt`, { method: "POST", body: form });
        if (!r.ok) throw new Error(r.status);
        const { transcript } = await r.json();
        if (!transcript.trim()) {
          setCallStatus("didn't catch that — try again", "");
          return;
        }
        await sendUserText(transcript);
      } catch (e) {
        setCallStatus("error — try again", "");
      }
    };
    mediaRecorder.start(100);
    recordingStartedAt = Date.now();
    els.talkBtn.classList.add("recording");
    els.talkLabel.textContent = "Release to send";
    setCallStatus("listening…", "listening");
  } catch (e) {
    alert("Mic access denied. Click the lock icon in the address bar and allow microphone.");
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    els.talkBtn.classList.remove("recording");
    els.talkLabel.textContent = "Hold to talk";
  }
}

// ---------- wiring ----------

function bindTalkButton() {
  const btn = els.talkBtn;
  btn.addEventListener("mousedown", startRecording);
  btn.addEventListener("mouseup", stopRecording);
  btn.addEventListener("mouseleave", stopRecording);
  btn.addEventListener("touchstart", e => { e.preventDefault(); startRecording(); });
  btn.addEventListener("touchend", e => { e.preventDefault(); stopRecording(); });
}

els.callBtn.addEventListener("click", startCall);
els.endBtn.addEventListener("click", () => endCall(false));
els.restartBtn.addEventListener("click", restartCall);
bindTalkButton();
loadBusinessName();
