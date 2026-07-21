# What a voice agent actually is

## The five moving parts

A voice agent is not one AI model. It's five things wired in a loop:

```
     caller says something
            |
            v
    ┌───────────────┐
    │ 1. TRANSPORT  │  phone, WhatsApp, browser, Telegram — how sound gets in and out
    └───────┬───────┘
            v
    ┌───────────────┐
    │ 2. STT        │  speech-to-text — turns audio bytes into a string
    └───────┬───────┘
            v
    ┌───────────────┐
    │ 3. BRAIN      │  the LLM, plus its state machine and tools
    └───────┬───────┘
            v
    ┌───────────────┐
    │ 4. TOOLS      │  check calendar, look up a customer, book, escalate
    └───────┬───────┘
            v
    ┌───────────────┐
    │ 5. TTS        │  text-to-speech — turns the reply string back into audio
    └───────┬───────┘
            v
     caller hears it
```

That loop runs many times per call — once per turn. Each stage can be swapped for a different provider or a local model. This repo is basically a well-organized set of adapters for each stage plus one brain that stays the same.

## What makes voice hard (that chat doesn't)

**Latency compounds.** In a text chat, waiting 3 seconds for a reply feels normal. On a phone call, 3 seconds of silence feels broken. The industry target is "first sound out of the speaker under 800ms after the caller stops talking." That means every stage — STT, LLM, TTS — needs to be measured and tuned. This is why streaming (start speaking before the LLM has finished thinking) matters.

**Turn-taking is subtle.** How do you know the caller finished a sentence? "Ummm, so… tomorrow, I guess?" — is that one turn or three? The pause detector (VAD, "voice activity detector") is a whole separate model that decides when to hand control to the STT.

**Barge-in / interruption.** If the caller starts talking while your agent is speaking, you need to stop mid-sentence and listen. Getting this right is what separates "toy" from "usable." Most managed platforms (Vapi, Retell) handle it for you.

**Audio formats.** A phone gives you µ-law 8kHz mono. WhatsApp sends OGG/Opus. Your local Kokoro spits out 24kHz WAV. Getting bytes into the right shape between stages is 20% of the code you write.

**Costs stack.** A 3-minute call touches STT (per minute), LLM (per token), TTS (per character), telephony (per minute), and the CRM webhook (usually free). None are expensive alone. Together they add up if you don't watch.

## Why now

Three things all landed within 12 months:

1. **Fast, cheap streaming STT** — Deepgram/Groq Whisper Turbo hit sub-300ms latency at fractions of a cent per minute.
2. **Function-calling LLMs got good** — GPT-4o, Claude, Gemini, Llama 3+ all do reliable tool calls. That's what makes "book me an appointment" actually work instead of "the AI hallucinated a fake time."
3. **Open TTS matched proprietary** — Chatterbox beat ElevenLabs in a public blind test. Kokoro-82M runs on a laptop. The "quality moat" of paid TTS mostly evaporated.

Result: you can now build something that competes with a $300/mo SaaS product and run it on your own laptop. That's why every small business is being pitched by an AI-receptionist agency this year.

## The two big product shapes

There are basically two ways to sell this:

**Shape A: managed voice platform (Vapi, Retell, Bland).**
- They own telephony + STT + TTS + orchestration. You write the "brain" and the tools.
- You pay per minute.
- Fastest to demo. Highest per-call cost. Great for < 10,000 minutes/mo.

**Shape B: self-hosted (LiveKit Agents, Pipecat).**
- You run everything on your own servers. You pay for cloud STT/LLM/TTS at raw rates.
- Slower to first demo. 60-80% cheaper past ~50k minutes/mo. Better story for privacy-sensitive clients (clinics, law firms).

This repo supports both. Same brain, different transport. You'd pick per client based on volume + privacy requirements.

## What "the brain" actually contains

Look at `packages/core_agent/brain.py`. It's about 150 lines. That's the whole product. Everything else in the repo is:
- Adapters to feed audio in
- Adapters to feed audio out
- Adapters to log or persist what happened
- Sample configurations per vertical (clinic, restaurant, real estate)

The brain does three things:
1. Runs one LLM call with the transcript + tool definitions.
2. If the LLM asked for a tool, runs it, feeds the result back, loops.
3. Once the LLM emits plain text, hands that to TTS and updates the extracted-fields JSON.

That's the whole magic. Everything else is glue.
