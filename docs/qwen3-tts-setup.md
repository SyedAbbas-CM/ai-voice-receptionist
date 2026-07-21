# Qwen3-TTS setup

Alibaba's open-weights TTS collection. Apache-2.0. Runs locally on GPU (or CPU with lower quality).

Collection: https://huggingface.co/collections/Qwen/qwen3-tts

## Which model to pick

| Model | Params | Use for |
|---|---|---|
| `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | 0.9B | Fast preset voices (Vivian, Adam, etc). Default. |
| `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 2B | Higher quality preset voices. |
| `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | 0.9B | Voice cloning from a reference sample. |
| `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | 2B | Higher-quality voice cloning. |
| `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 2B | Prompt-driven voice design ("a warm middle-aged doctor"). |

Weights download automatically on first use to `~/.cache/huggingface/hub/`.

## Install

```bash
pip install qwen-tts torch soundfile
# Optional for lower VRAM:
MAX_JOBS=4 pip install -U flash-attn --no-build-isolation
```

## Configure

Preset voice mode:
```
TTS_PROVIDER=qwen3
QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
QWEN3_TTS_DEVICE=cuda:0
QWEN3_TTS_DEFAULT_SPEAKER=Vivian
QWEN3_TTS_DEFAULT_LANGUAGE=English
QWEN3_TTS_INSTRUCT=warm, professional receptionist tone
```

Voice cloning mode (requires a Base model):
```
TTS_PROVIDER=qwen3
QWEN3_TTS_MODEL_ID=Qwen/Qwen3-TTS-12Hz-0.6B-Base
QWEN3_TTS_REF_AUDIO=/absolute/path/to/reference.wav
QWEN3_TTS_REF_TEXT=Hello, this is the reference sample I'm cloning from.
```

**Legal note:** never clone a voice without the person's written consent.

## Hardware guidance

- **0.6B on NVIDIA (3090 etc.)**: bfloat16, ~2-3GB VRAM. Fast — expect <1s per sentence.
- **1.7B on NVIDIA**: bfloat16, ~4-5GB VRAM. Also fast.
- **Apple Silicon (M-series)**: `QWEN3_TTS_DEVICE=mps`, `QWEN3_TTS_DTYPE=float32`.
  - **Important**: float16 and bfloat16 both cause NaN/Inf in the sampler on MPS. The adapter auto-forces float32 on MPS.
  - Measured on M1 Mac / 16GB RAM: 0.6B model loads in ~15s (cached), synth ~40s for a 15-word sentence. Fine for testing, too slow for real calls.
  - For phone-call use on Mac, either put the TTS on a Linux GPU box, or use a smaller/faster model (Kokoro, Piper) locally.
- **CPU only**: works with the 0.6B model but very slow (~1-2 min per sentence). Only for debugging.

### Verify it works

We shipped a smoke test:
```bash
source .venv/bin/activate
python scripts/test_qwen3_tts.py
open data/qwen3_smoke.wav      # macOS
```
On a first run this downloads ~1.8GB to `~/.cache/huggingface/hub/`. On subsequent runs it just loads from cache.

## First-run cost

Model download is ~1.8GB (0.6B) or ~3.4GB (1.7B) plus the shared tokenizer (~400MB). One-time.

## Verify

```bash
curl -sS -X POST http://localhost:8000/v1/text-to-speech/Vivian \
  -H "Content-Type: application/json" \
  -d '{"text":"Hi, thanks for calling. How can I help you today?"}' \
  --output test.wav
open test.wav
```
