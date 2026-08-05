// Manages the two WebSockets and the audio pipe for a single call.
//
// WS #1 (/twilio/stream): audio protocol identical to Twilio Media Streams.
// WS #2 (/debug/live?call_id=...): server-pushed intelligence events for
// the right-side debug panel.

import { AudioPipe } from './audio-pipe.js';

function makeCallId() {
  // 8 hex chars — matches Twilio callSid length loosely; the browser
  // path prefixes with `browser_` so it's obvious in logs.
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return 'browser_' + Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

export class CallStreamSession {
  constructor() {
    this._ws = null;         // audio ws
    this._debugWs = null;    // debug ws
    this._pipe = null;
    this._callId = null;
    this._streamSid = null;
    this._callbacks = {};
    this._startedAt = 0;
    this._turnCount = 0;
    this._pendingMarks = [];  // mark names sent by server, awaiting ack
  }

  async startCall(callbacks) {
    this._callbacks = callbacks || {};
    this._callId = makeCallId();
    this._streamSid = this._callId;
    this._startedAt = Date.now();
    this._turnCount = 0;

    const host = window.location.host || '127.0.0.1:8000';
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const audioUrl = `${proto}://${host}/twilio/stream`;
    const debugUrl = `${proto}://${host}/debug/live?call_id=twilio_${this._callId}`;

    this._ws = new WebSocket(audioUrl);
    this._ws.binaryType = 'arraybuffer';
    // If the socket opens synchronously before we attach onopen we'd hang
    // forever — check readyState after registering the handler.
    await new Promise((resolve, reject) => {
      if (this._ws.readyState === WebSocket.OPEN) { resolve(); return; }
      if (this._ws.readyState === WebSocket.CLOSED) { reject(new Error('ws closed before open')); return; }
      this._ws.addEventListener('open', () => resolve(), { once: true });
      this._ws.addEventListener('error', (e) => reject(new Error('ws error before open')), { once: true });
    });
    this._setStatus('connected');

    // Send Twilio-format handshake so the server's actor path accepts it.
    this._ws.send(JSON.stringify({
      event: 'connected', protocol: 'Call', version: '1.0.0',
    }));
    this._ws.send(JSON.stringify({
      event: 'start',
      sequenceNumber: '1',
      start: {
        streamSid: this._streamSid,
        accountSid: 'browser-widget',
        callSid: this._callId,
        tracks: ['inbound'],
        mediaFormat: { encoding: 'audio/x-mulaw', sampleRate: 8000, channels: 1 },
        customParameters: { origin: 'browser' },
      },
    }));

    // Route server-to-client events.
    this._ws.onmessage = (ev) => this._handleServerMessage(ev);
    this._ws.onclose = () => this._onWsClosed('audio');
    this._ws.onerror = (e) => console.warn('audio ws error', e);

    // Boot the audio pipe (mic + speaker).  Frames go straight to the WS.
    this._pipe = new AudioPipe();
    let mediaSeq = 2;
    // Sprint 12: mic stays HOT the entire call so barge-in works.
    // The browser's built-in AEC (echoCancellation: true in getUserMedia)
    // handles most speaker→mic feedback.  Any residual echo the audit
    // pipeline sees is safer to handle server-side (backchannel
    // classifier, ledger reconciliation) than by muting the mic.
    await this._pipe.start((mulawBytes) => {
      if (this._ws && this._ws.readyState === 1) {
        const b64 = btoa(String.fromCharCode(...mulawBytes));
        this._ws.send(JSON.stringify({
          event: 'media',
          sequenceNumber: String(mediaSeq++),
          media: { track: 'inbound', chunk: String(mediaSeq), timestamp: String(Date.now()), payload: b64 },
        }));
      }
    });

    // When the local playback queue drains, ack any pending marks
    // back to the server so the ledger advances.
    this._pipe.onPlaybackCaughtUp(() => {
      while (this._pendingMarks.length > 0) {
        const name = this._pendingMarks.shift();
        if (this._ws && this._ws.readyState === 1) {
          this._ws.send(JSON.stringify({
            event: 'mark',
            streamSid: this._streamSid,
            mark: { name },
          }));
        }
      }
    });

    // Debug event stream.  Opens after audio so we don't miss greeting events.
    this._openDebugWs(debugUrl);
    this._setStatus('agent speaking');
  }

  _openDebugWs(url) {
    this._debugWs = new WebSocket(url);
    this._debugWs.onmessage = (ev) => {
      try {
        const row = JSON.parse(ev.data);
        if (this._callbacks.onDebugEvent) this._callbacks.onDebugEvent(row);
        // Extract user-visible transcript bubbles from the raw event stream.
        this._maybeSurfaceTranscript(row);
      } catch (_) {}
    };
    this._debugWs.onclose = () => {
      // Silent reconnect after 5s if the call is still live.
      if (this._ws && this._ws.readyState === 1) {
        setTimeout(() => this._openDebugWs(url), 5000);
      }
    };
    this._debugWs.onerror = () => {};
  }

  _maybeSurfaceTranscript(row) {
    if (!this._callbacks.onTranscript) return;
    // User final transcript
    if (row.source === 'stt' && row.kind === 'final' && row.payload && row.payload.text) {
      this._callbacks.onTranscript({ who: 'user', text: row.payload.text });
      this._turnCount++;
    }
    // Agent utterance (control.tts_utterance is what the actor emits)
    if (row.source === 'tts' && row.kind === 'utterance' && row.payload && row.payload.text) {
      this._callbacks.onTranscript({ who: 'agent', text: row.payload.text });
      this._turnCount++;
    }
  }

  _handleServerMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.event) {
      case 'media':
        if (msg.media && msg.media.payload) {
          const bin = atob(msg.media.payload);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          const fmt = (msg.media && msg.media.format) || '';
          const m = fmt.match(/^pcm_s16le_(\d+)$/);
          if (m) {
            this._pipe.playPcmFrame(bytes, parseInt(m[1], 10));
          } else {
            // No format marker → legacy Twilio µ-law 8kHz.
            this._pipe.playMulawFrame(bytes);
          }
        }
        break;
      case 'mark':
        if (msg.mark && msg.mark.name) {
          this._pendingMarks.push(msg.mark.name);
        }
        break;
      case 'clear':
        this._pipe.handleClear();
        this._pendingMarks = [];
        if (this._callbacks.onDebugEvent) {
          this._callbacks.onDebugEvent({
            source: 'control', kind: 'clear (barge)',
            payload: {}, wall_ts: Date.now() / 1000,
          });
        }
        break;
      case 'stop':
        this._onWsClosed('server-stop');
        break;
    }
  }

  _onWsClosed(reason) {
    // Idempotent — fires from ws.onclose or explicit endCall.
    if (this._callbacks.onEnded) {
      const durationSec = Math.round((Date.now() - this._startedAt) / 1000);
      this._callbacks.onEnded({ durationSec, turnCount: this._turnCount, reason });
      this._callbacks.onEnded = null;  // dedupe
    }
    this._teardown();
  }

  _teardown() {
    if (this._pipe) { this._pipe.stop(); this._pipe = null; }
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
    if (this._debugWs) { try { this._debugWs.close(); } catch (_) {} this._debugWs = null; }
  }

  _setStatus(text) {
    if (this._callbacks.onStatus) this._callbacks.onStatus(text);
  }

  endCall() {
    if (this._ws && this._ws.readyState === 1) {
      try { this._ws.send(JSON.stringify({ event: 'stop', sequenceNumber: '99' })); } catch (_) {}
    }
    this._onWsClosed('user-hangup');
  }
}
