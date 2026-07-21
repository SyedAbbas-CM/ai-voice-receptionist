# Reading list

Ranked by "if I could only read three things this week." Skip anything not on this list — the voice-AI blog space is 80% noise.

## Tier 1 — read these first (~2 hours)

1. **[LiveKit — "The state of voice agents in 2026"](https://blog.livekit.io/state-of-voice-agents-2026/)**
   Best single overview of the space. Latency budgets, architecture patterns, cost benchmarks. Read this even if you'll never use LiveKit.

2. **[Deepgram — "The voice agent latency guide"](https://deepgram.com/learn/voice-agent-latency)**
   Concrete numbers per pipeline stage. What "sub-800ms first response" actually requires.

3. **[Twilio Media Streams docs](https://www.twilio.com/docs/voice/media-streams)**
   You'll implement or debug this eventually. Skim the "connecting" and "messages" sections. The µ-law WebSocket protocol is simple once you see the JSON shape.

4. **[Vapi custom-LLM docs](https://docs.vapi.ai/customization/custom-llm/using-your-server)**
   Our `routes/vapi.py` implements this shape. Reading the source docs lets you extend to Retell/Bland (they use the same OpenAI-compatible pattern).

## Tier 2 — read when you build the local stack

5. **[Kokoro-82M model card](https://huggingface.co/hexgrad/Kokoro-82M)**
   The default open-source TTS. 82M params, Apache 2.0. Runs on CPU. Read the "how to use" section.

6. **[Qwen3-TTS collection](https://huggingface.co/collections/Qwen/qwen3-tts)**
   What we integrated. Six variants across preset voices, cloning, and prompt-based voice design.

7. **[Chatterbox](https://github.com/resemble-ai/chatterbox)**
   The one that beat ElevenLabs in Resemble AI's blind test (65% vs 24%). MIT license. When quality is the demo hook, use this.

8. **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**
   Local Whisper on CPU or GPU. What we use for offline STT. Real-world 5-10× faster than the reference PyTorch Whisper.

9. **[Silero VAD](https://github.com/snakers4/silero-vad)**
   The 1MB voice-activity detector. If you build streaming STT, you'll use this.

## Tier 3 — read when you go self-hosted

10. **[LiveKit Agents docs](https://docs.livekit.io/agents/)**
    The Vapi alternative. Realtime voice agent framework. SIP trunk integration.

11. **[Pipecat docs](https://docs.pipecat.ai/)**
    The other Vapi alternative. Python-first. Cleaner code, smaller community.

12. **[OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime)**
    Speech-to-speech in a single websocket. Best latency, highest cost. Read when a client asks for "the fastest possible" agent.

## Tier 4 — background theory

13. **[WhisperX paper](https://arxiv.org/abs/2303.00747)** — how modern Whisper adds forced alignment for word-level timestamps. Useful when you build barge-in.

14. **[The Attention Is All You Need paper](https://arxiv.org/abs/1706.03762)** — the transformer architecture underlying every LLM and every modern TTS model. One read is enough.

15. **[VITS paper](https://arxiv.org/abs/2106.06103)** — the architecture behind XTTS, Piper, and many local TTS models. Skim if curious about *why* they work.

## What NOT to read

- **LangChain / LlamaIndex tutorials for voice** — wrong abstraction layer. Adds latency, hides the pipeline, breaks in production.
- **"Build a voice agent with Bolt.new / Cursor / v0" clickbait** — these teach the interface, not the concepts. Fine for demos, terrible for shipping.
- **YouTube "I made $10k/mo with AI voice agents" videos** — sales pitches. If they show real code, watch on 2× and skip to that.
- **Medium articles from 2023-2024 about "which voice API to pick"** — everything has changed. Use posts dated 2026+.

## Communities worth being in

- **LiveKit Discord** — official but active. Real engineers, fast answers.
- **r/LocalLLaMA** — TTS/STT posts land here first. Filter for `[TTS]` and `[STT]` flair.
- **HuggingFace TTS Arena Leaderboard** — [huggingface.co/spaces/TTS-AGI/TTS-Arena](https://huggingface.co/spaces/TTS-AGI/TTS-Arena) — blind-test rankings updated regularly. Look here before picking a new voice.

## The three things worth watching yourself

1. **Deepgram vs Groq Whisper vs OpenAI Whisper** — record yourself, transcribe with all three, compare. You'll never guess which one wins on your accent/domain.
2. **ElevenLabs vs Kokoro vs Chatterbox vs Qwen3** — synthesize the same paragraph with all four, listen with headphones. You'll pick a favorite that's not the "highest MOS."
3. **Your own call end-to-end** — record a call to your Twilio number, save the WAV of the µ-law stream, listen to what your STT actually receives. You'll immediately spot the "why did it mishear that" problems.

## When you hit a wall

- **Latency too high** → measure per stage first. Don't optimize the LLM if 900ms of your budget is TTS.
- **Agent hallucinates a booking** → your tool definitions are too loose. Add required fields, examples, and a `check_availability` that must be called first.
- **Caller keeps interrupting the agent** → speed up your TTS (turbo/flash models), shorten the LLM replies (max 1-2 sentences in the system prompt).
- **Model won't shut up / rambles** → drop temperature to 0.1, add `Keep responses to one sentence.` in the system prompt.
