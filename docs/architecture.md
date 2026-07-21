# Architecture

## Layered view

```
                 transport             VAD           STT            LLM           tools          TTS
phone/browser -> browser/twilio/    -> silero/    -> deepgram/   -> openai/    -> calendar/   -> elevenlabs/
                 livekit/vapi         transport      groq/local     claude/       crm/faq        openai/local
                                                                    gemini/                       browser
                                                                    ollama
                                                  |
                                                  v
                                            session manager
                                                  |
                                                  v
                                       SQLite (sessions, transcript, bookings)
                                                  |
                                                  v
                                          dashboard / API
```

## Phase 2 (current) — browser sim

1. Browser captures mic with `MediaRecorder` (WebM/Opus).
2. POST `/voice/stt` with the audio blob; backend routes to the selected STT provider.
3. POST `/chat/turn` with the transcribed text; backend runs the receptionist brain:
   - appends the user turn to `CallState`
   - calls the LLM with the system prompt + transcript + tool definitions
   - dispatches any tool calls (max 4 iterations)
   - re-extracts structured fields with a JSON-only call
4. POST `/voice/tts-base64` with the reply text; backend returns either base64 audio or a sentinel telling the browser to use built-in SpeechSynthesis.

Session state lives in-memory keyed by `session_id` and is mirrored to SQLite on every turn so the dashboard sees live progress.

## Provider swapping

All cloud calls go through abstract base classes in `apps/api/app/providers/base.py`. The factory in `factory.py` picks the implementation based on env vars. To add a new provider, drop a file under `providers/{stt,llm,tts}/`, subclass the base, and register it in the factory dispatch.

## Phase 3 — Vapi webhook

Replace the `transport/browser_webrtc` path with `transport/vapi_webhook`. The brain and tools stay identical. The webhook handler will convert Vapi's payload shape into a `CallState` turn and stream back tool results in Vapi's expected response shape.

## Phase 4 — Twilio + OpenAI Realtime

A separate WebSocket route proxies Twilio Media Streams to OpenAI Realtime. Tool calls still route to the same `ClinicToolHandler`. This branch is the "real phone call without Vapi" demo.

## Phase 5 — LiveKit / Pipecat

LiveKit Agents (or Pipecat pipeline) hosts the session. The brain is wrapped as a LiveKit agent participant. SIP trunk via LiveKit's SIP service handles inbound PSTN.
