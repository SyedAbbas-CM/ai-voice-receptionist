"""Generate a battery of receptionist lines in the cloned voice.

Run AFTER `scripts/test_qwen3_tts_clone.py` succeeds (model is cached, clone works).
Writes six WAVs to data/clone_battery/ so we can audition the cloned voice against
realistic call turns before booting the server.

Run:
    source .venv/bin/activate
    python scripts/qwen3_clone_receptionist_battery.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel


MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
REF_AUDIO = str(REPO_ROOT / "data" / "voice_sample.wav")
REF_TEXT = (
    "Today I'm recording a cleaner reference for the voice pipeline. "
    "I want this read to stay grounded, calm, and a little deeper than my normal speaking voice. "
    "The goal is not to sound dramatic. The goal is to sound controlled, clear, and intentional. "
    "This line should feel neutral and explanatory. I'm just describing what's happening without trying too hard. "
    "This line should feel mildly annoyed, like something in the game is obviously broken and I have to deal with it again. "
    "This line should feel deadpan. Not bored, not sleepy, just flat on purpose, like the joke is in how little I react. "
    "This line should feel reactive. Wait, look at this. He's spamming way too many bombs already. "
    "This line should feel dry and sarcastic. Yeah, this is definitely working exactly how I planned it. "
    "This line should feel baffled. I genuinely don't know why the boss keeps doing that. "
    "This line should feel reflective. The project still has many problems, but the bones are good. "
    "If this reference works, it should help the model separate calm explanation from reaction, sarcasm, annoyance, and quiet confidence."
)

LINES = [
    ("greeting", "Hi, thanks for calling Riverside Family Clinic. This is Alex — how can I help you today?"),
    ("confirm_booking", "Got it. I have you down for Tuesday at ten a.m. with Doctor Chen. Anything else?"),
    ("faq_insurance", "Yes, we do take Aetna. We also accept Blue Cross and United Healthcare."),
    ("faq_hours", "We're open weekdays nine to six, and Saturdays until noon."),
    ("filler", "One second, let me check that for you."),
    ("escalation", "That's a good question — let me get one of the nurses to call you back on this."),
]


def pick_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if torch.backends.mps.is_available():
        return "mps", torch.float32
    return "cpu", torch.float32


def main() -> int:
    device, dtype = pick_device_and_dtype()
    print(f"[battery] device={device} dtype={dtype}")
    print(f"[battery] loading {MODEL_ID}")
    t0 = time.time()
    try:
        model = Qwen3TTSModel.from_pretrained(
            MODEL_ID, device_map=device, dtype=dtype,
            attn_implementation="flash_attention_2",
        )
    except Exception:
        model = Qwen3TTSModel.from_pretrained(MODEL_ID, device_map=device, dtype=dtype)
    print(f"[battery] model loaded in {time.time() - t0:.1f}s")

    out_dir = REPO_ROOT / "data" / "clone_battery"
    out_dir.mkdir(parents=True, exist_ok=True)

    total_synth = 0.0
    total_audio = 0.0
    for slug, text in LINES:
        print(f"[battery] {slug}: {text!r}")
        t0 = time.time()
        wavs, sr = model.generate_voice_clone(
            text=text,
            language="English",
            ref_audio=REF_AUDIO,
            ref_text=REF_TEXT,
        )
        dt = time.time() - t0
        audio_s = len(wavs[0]) / sr
        rtf = dt / audio_s if audio_s > 0 else float("inf")
        total_synth += dt
        total_audio += audio_s
        out = out_dir / f"{slug}.wav"
        sf.write(str(out), wavs[0], sr)
        print(f"           {dt:.1f}s synth -> {audio_s:.1f}s audio (RTF {rtf:.2f}) -> {out.name}")

    overall_rtf = total_synth / total_audio if total_audio > 0 else float("inf")
    print(f"[battery] done. {len(LINES)} lines. overall RTF: {overall_rtf:.2f}")
    print(f"[battery] play: open {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
