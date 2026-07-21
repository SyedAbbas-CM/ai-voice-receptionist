# Provider matrix

Pick a row per column. Flip the env var, restart the API, done.

| Level | LLM | STT | TTS | Transport | CRM sink | Use when |
|-------|-----|-----|-----|-----------|----------|----------|
| 0 — fully local | `ollama` (qwen2.5/llama3.1) | `local` (faster-whisper) | `local` (piper) or `browser` | browser | `none` | unlimited demos, no API cost |
| 1 — cheap hybrid | `groq` or `gemini` | `groq` (whisper-v3-turbo) | `deepgram` (aura) | browser | `sheets` | daily testing on free credits |
| 2 — Upwork demo | `openai` (gpt-4o-mini) | `deepgram` (nova-3) | `elevenlabs` (turbo v2.5) | vapi | `sheets` or `ghl` | client-facing booking demos |
| 3 — GHL agency | `openai` (gpt-4o-mini) | Vapi-managed (deepgram) | Vapi-managed (elevenlabs) | vapi | `ghl` | GHL agencies wanting AI receptionist per sub-account |
| 4 — custom build | `anthropic` or `openai` | `deepgram` or `openai` | `elevenlabs` or `cartesia` | livekit + twilio SIP | `ghl+sheets` | higher-ticket client deployments |

## Notes on each provider

- **OpenAI**: best tool-calling quality, easy to demo. Pricier for long testing.
- **Groq**: free tier is generous (Llama 3.3 70B + Whisper-v3-turbo). Tool calling supported. Use as default for daily dev.
- **Gemini Flash**: cheap, fast, supports function calling. Free tier may use data for training — disable for client demos.
- **Anthropic**: best for high-stakes call flows (medical, legal). Tool calling is robust.
- **Ollama**: only for local/private deployments and offline demos. Use a tool-calling capable model like `qwen2.5:7b` or `llama3.1:8b`.
- **Deepgram**: $200 free credit, sub-300ms streaming STT.
- **ElevenLabs**: best voice quality for clinic/restaurant demos. Watch credit burn on long testing.
- **Cartesia**: lowest-latency hosted TTS, free tier exists.
- **Piper local**: very fast, runs on CPU, simple to package. Voice quality is acceptable for internal demos.
- **`browser` TTS sentinel**: zero-cost fallback. The backend returns a sentinel and the simulator uses `window.speechSynthesis`. Useful for the first Loom.
