// AudioWorkletProcessor: downsamples input from AudioContext sample rate
// to 8000 Hz and encodes to µ-law bytes. Posts a Uint8Array every 20 ms
// (160 samples at 8000 Hz) back to the main thread.
//
// The linear-to-µlaw conversion is the standard ITU-T G.711 algorithm.

const TARGET_RATE = 8000;
const FRAME_SAMPLES = 160; // 20 ms at 8000 Hz

function linear2ulaw(sample) {
  // Clamp to int16 range.
  let s = Math.max(-32768, Math.min(32767, sample | 0));
  const sign = (s >> 8) & 0x80;
  if (sign) s = -s;
  if (s > 32635) s = 32635;
  s = s + 0x84;
  let exponent = 7;
  for (let mask = 0x4000; (s & mask) === 0 && exponent > 0; exponent--, mask >>= 1);
  const mantissa = (s >> (exponent + 3)) & 0x0f;
  const ulaw = ~(sign | (exponent << 4) | mantissa) & 0xff;
  return ulaw;
}

class MulawEncoderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._resampleBuf = [];       // downsampled 8kHz float samples awaiting frame emit
    this._srcAccum = 0;            // fractional accumulator for downsampling
    this._srcRate = sampleRate;    // AudioWorkletGlobalScope provides this
    this._ratio = this._srcRate / TARGET_RATE;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const src = input[0];  // mono channel 0, Float32Array length ~128

    // Boost input by 10x — laptop mics deliver very quiet signal
    // (peaks around 0.02-0.05) and Deepgram needs meaningful amplitude
    // (peaks > 0.1) to fire SpeechStarted reliably.
    const GAIN = 10.0;

    // Simple nearest-sample downsample. Good enough for speech at
    // 8 kHz; STT won't notice the anti-alias absence in practice.
    for (let i = 0; i < src.length; i++) {
      this._srcAccum += 1;
      if (this._srcAccum >= this._ratio) {
        this._srcAccum -= this._ratio;
        // Convert Float32 (-1..1) to int16 range then µ-law.
        const boosted = Math.max(-1, Math.min(1, src[i] * GAIN));
        const s16 = boosted * 32767;
        this._resampleBuf.push(linear2ulaw(s16));
      }
    }

    // Emit frames of FRAME_SAMPLES bytes to the main thread.
    while (this._resampleBuf.length >= FRAME_SAMPLES) {
      const frame = new Uint8Array(this._resampleBuf.splice(0, FRAME_SAMPLES));
      // Transferable so we don't copy: hand ownership of the buffer.
      this.port.postMessage({ mulaw: frame }, [frame.buffer]);
    }
    return true;
  }
}

registerProcessor('mulaw-encoder-worklet', MulawEncoderProcessor);
