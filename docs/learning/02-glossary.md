# Glossary

Every acronym you'll trip over. Sorted alphabetical after the first cluster.

## The pipeline stages

- **STT** — Speech-to-text. Audio in, string out. Whisper, Deepgram Nova, AssemblyAI. Measured in words-per-minute accuracy and latency.
- **LLM** — Large language model. The brain that decides what to say back. GPT-4o, Claude, Llama, Gemini, Qwen.
- **TTS** — Text-to-speech. String in, audio out. ElevenLabs, Kokoro, Qwen3-TTS, Piper.
- **VAD** — Voice activity detector. A tiny model that says "yes there's speech in this audio chunk / no it's silence." Silero VAD is the default. Runs in <1ms per chunk. This is what tells your STT when a turn ended.
- **Barge-in** — Caller talks while the agent is talking. A good system stops the TTS and starts listening. Managed platforms (Vapi, Retell) do this for you. Self-hosted, you wire it yourself.

## Audio formats

- **PCM** — Pulse-code modulation. Raw uncompressed audio samples. Usually 16-bit integers.
- **µ-law / mulaw / G.711** — Compressed 8-bit codec. What telephony uses. 8kHz mono. Every phone call in North America.
- **Opus** — Modern lossy codec. What WebRTC, WhatsApp voice notes, and Discord use. Better quality than µ-law at similar bitrates.
- **WAV** — A container format wrapping PCM. What most local TTS models emit.
- **MP3** — Container for MPEG-1 layer 3 compressed audio. What ElevenLabs and OpenAI TTS emit by default.
- **OGG** — Container often wrapping Opus. What WhatsApp and Telegram voice messages come in.
- **Sample rate** — Samples per second. 8kHz = telephone quality. 16kHz = STT sweet spot. 24kHz = decent TTS. 44.1kHz / 48kHz = studio.
- **Bit depth** — Bits per sample. 8-bit = phone. 16-bit = CD. 24-bit = studio.

## Telephony

- **PSTN** — Public Switched Telephone Network. The actual phone system.
- **SIP** — Session Initiation Protocol. How VoIP calls are set up (like HTTP for phone calls).
- **SIP trunk** — A connection from your app to a SIP-speaking phone carrier. Twilio has one. LiveKit has one.
- **DTMF** — Dual-tone multi-frequency. The beeps when you press keys on a phone (`1`, `2`, `#`). Used for menu inputs.
- **TwiML** — Twilio Markup Language. XML that tells Twilio "answer the call, then do X." Similar to how HTML tells a browser what to render.
- **Media Streams** — Twilio's WebSocket protocol that streams call audio to your server as base64 µ-law frames.

## Realtime / streaming

- **WebRTC** — Web Real-Time Communication. Browsers speak this natively. LiveKit is built on it. Sub-100ms latency, peer-to-peer or through a SFU.
- **WebSocket** — Long-lived HTTP-upgraded connection. Twilio Media Streams and OpenAI Realtime both use this to stream audio.
- **SFU** — Selective Forwarding Unit. A server that relays WebRTC streams (LiveKit is one).
- **Streaming STT** — STT that emits partial transcripts as you speak, instead of only at end-of-turn. Cuts perceived latency by 200-500ms.
- **Streaming TTS** — TTS that starts emitting audio while still generating. First audio out under 400ms is possible.

## LLM stuff you'll see in voice code

- **Function calling / tool use** — The LLM returns a structured "call this function with these args" instead of plain text. This is how the brain books appointments, checks calendars, escalates.
- **System prompt** — The persistent instructions at the top of the conversation. Sets persona, tools, escalation rules.
- **Context window** — How much conversation history the LLM can see at once. 128k tokens is standard now.
- **Temperature** — 0 = deterministic, 1 = creative. Voice agents run at 0.2–0.4 — you want consistent booking flow, not creative writing.
- **Tokens** — Sub-word chunks. Roughly 1 token = 4 English characters. Billing unit for most LLMs.

## Voice model concepts

- **MOS** — Mean Opinion Score. Human-rated audio quality on a 1–5 scale. ElevenLabs Turbo v2.5 ~ 4.8. Kokoro-82M ~ 4.5. Anything above 4 sounds "good" to most callers.
- **CER / WER** — Character/word error rate. Lower is better. Used to score STT and sometimes TTS via a round-trip.
- **Voice cloning** — Producing speech in a target person's voice from a short reference sample. 3-30 seconds is usually enough for modern models (XTTS, Chatterbox, Qwen3-Base).
- **Zero-shot cloning** — Cloning without any fine-tuning on the reference. Just pass a WAV, get a voice.
- **Instruction TTS / prompted TTS** — TTS that takes a natural-language style instruction ("read this in a whisper", "excited news anchor"). Qwen3-TTS-VoiceDesign and F5-TTS do this.
- **Speaker embedding** — A fixed-length vector that represents "how someone sounds." Used internally by cloning models.

## Platform / integration

- **Cloud API vs Business API** (WhatsApp) — Meta's hosted vs on-premise WhatsApp integration. Cloud API is what you want.
- **Webhook** — HTTP endpoint the platform POSTs to when something happens (message received, call started, etc).
- **CRM** — Customer relationship management. Where contacts and deals live. GoHighLevel, HubSpot, Salesforce, Airtable.
- **GHL** — GoHighLevel. Popular white-labeled CRM for agencies. Not to be confused with GHZ (the frequency).
- **SIP trunk provider** — Company that sells you SIP endpoints connected to PSTN. Twilio, Telnyx, Signalwire.

## What Vapi / Retell / Bland actually are

Not AI. **Orchestrators.** They:
- Own the SIP trunk to Twilio.
- Buffer the µ-law from the phone.
- Send it to Deepgram/OpenAI Whisper for STT.
- Send the transcript to your LLM (or theirs).
- Send the reply to ElevenLabs for TTS.
- Stream the audio back to the caller.
- Handle barge-in.

They charge you a per-minute markup for saving you the "wire it yourself" work. LiveKit Agents and Pipecat are the open-source versions.

## What LangChain / LlamaIndex are NOT

Not needed for voice agents. Their abstraction layers add latency and hide what's happening. Skip them for this stack. Straight `httpx` calls to OpenAI / Groq / Anthropic are cleaner and faster. If you see a voice-agent tutorial that starts with LangChain, close the tab.
