// Bidirectional µ-law 8kHz audio pipeline for the browser call widget.
//
// Upstream: mic → AudioWorklet (mulaw-worklet.js) → 20ms µ-law frames
// → caller-provided send fn.
//
// Downstream: caller invokes playMulawFrame(bytes); we decode µ-law
// to Float32 PCM at 8kHz and schedule a BufferSource on a shared
// AudioContext with a small look-ahead so playback stays glitch-free.

const TARGET_RATE = 8000;

function ulaw2linear(u) {
  u = ~u & 0xff;
  const sign = u & 0x80;
  const exponent = (u >> 4) & 0x07;
  const mantissa = u & 0x0f;
  let sample = ((mantissa << 3) + 0x84) << exponent;
  sample -= 0x84;
  return sign ? -sample : sample;
}

export class AudioPipe {
  constructor() {
    this._ctx = null;
    this._micStream = null;
    this._workletNode = null;
    this._srcNode = null;
    this._sendFn = null;
    this._nextStartAt = 0;
    this._pendingSources = [];
    this._caughtUpCbs = [];
    this._caughtUpTimer = null;
  }

  async start(sendMulawFrame) {
    this._sendFn = sendMulawFrame;
    // A user-gesture-triggered AudioContext for both directions.
    // 8000 rate is what we want for our decoded playback; browsers may
    // choose to run the mic input at a native rate and we downsample in the worklet.
    this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (this._ctx.state === 'suspended') await this._ctx.resume();

    // Downstream playback timing anchor: give ourselves 50ms of pad.
    this._nextStartAt = this._ctx.currentTime + 0.05;

    // Upstream mic → worklet.
    this._micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    await this._ctx.audioWorklet.addModule('./mulaw-worklet.js');
    this._srcNode = this._ctx.createMediaStreamSource(this._micStream);
    this._workletNode = new AudioWorkletNode(this._ctx, 'mulaw-encoder-worklet');
    this._workletNode.port.onmessage = (ev) => {
      if (this._sendFn && ev.data && ev.data.mulaw) {
        this._sendFn(ev.data.mulaw);
      }
    };
    this._srcNode.connect(this._workletNode);
    // The worklet doesn't need to be audible — but you MUST connect it
    // somewhere or Chrome garbage-collects the audio graph.  Use a
    // gain node with zero gain as a sink.
    const silentSink = this._ctx.createGain();
    silentSink.gain.value = 0;
    this._workletNode.connect(silentSink).connect(this._ctx.destination);
  }

  playMulawFrame(mulawBytes) {
    if (!this._ctx) return;
    const nSamples = mulawBytes.length;
    // Decode µ-law → int16 → Float32.
    const buf = this._ctx.createBuffer(1, nSamples, TARGET_RATE);
    const chan = buf.getChannelData(0);
    for (let i = 0; i < nSamples; i++) {
      chan[i] = ulaw2linear(mulawBytes[i]) / 32768;
    }
    const src = this._ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this._ctx.destination);
    const startAt = Math.max(this._ctx.currentTime + 0.02, this._nextStartAt);
    src.start(startAt);
    this._nextStartAt = startAt + buf.duration;
    this._pendingSources.push({ src, endsAt: this._nextStartAt });
    this._scheduleCaughtUpCheck();
  }

  handleClear() {
    // Cancel every pending playback source (barge-in from server).
    for (const { src } of this._pendingSources) {
      try { src.stop(0); } catch (_) {}
    }
    this._pendingSources = [];
    if (this._ctx) this._nextStartAt = this._ctx.currentTime + 0.02;
  }

  onPlaybackCaughtUp(cb) {
    this._caughtUpCbs.push(cb);
  }

  _scheduleCaughtUpCheck() {
    if (this._caughtUpTimer) return;
    const check = () => {
      if (!this._ctx) return;
      // Reap finished sources.
      const now = this._ctx.currentTime;
      this._pendingSources = this._pendingSources.filter(p => p.endsAt > now);
      if (this._pendingSources.length === 0) {
        this._caughtUpTimer = null;
        for (const cb of this._caughtUpCbs) {
          try { cb(); } catch (_) {}
        }
        return;
      }
      this._caughtUpTimer = setTimeout(check, 40);
    };
    this._caughtUpTimer = setTimeout(check, 40);
  }

  async stop() {
    if (this._workletNode) { try { this._workletNode.disconnect(); } catch (_) {} }
    if (this._srcNode)     { try { this._srcNode.disconnect();     } catch (_) {} }
    if (this._micStream)   { this._micStream.getTracks().forEach(t => t.stop()); }
    if (this._ctx)         { try { await this._ctx.close();         } catch (_) {} }
    this._ctx = null;
    this._micStream = null;
    this._workletNode = null;
    this._srcNode = null;
    this._pendingSources = [];
    this._caughtUpCbs = [];
    if (this._caughtUpTimer) { clearTimeout(this._caughtUpTimer); this._caughtUpTimer = null; }
  }
}
