# Complete reading order — start to finish

Every doc and every code file in the repo, listed in the order you should read them. If you follow this list top-to-bottom, you'll understand the whole system.

Time estimate for the full path: ~4-5 hours of reading + ~1 hour of hands-on. You do not need to do it all in one sitting.

Legend:
- **[read]** — read carefully
- **[skim]** — 60 seconds, just know it exists
- **[do]** — hands-on, follow the steps
- **[ref]** — reference, don't read cover-to-cover; open when you need it

---

## Stage 0 — Orient yourself (10 min)

1. **[read]** `README.md`
   The one-page overview. What this repo is and isn't.

2. **[read]** `docs/learning/00-start-here.md`
   The reading map. Confirms you're on the right path.

## Stage 1 — Core concepts (30 min)

3. **[read]** `docs/learning/01-what-is-a-voice-agent.md`
   The five moving parts. Why voice is harder than chat. Why now. What "the brain" actually contains.

4. **[read]** `docs/learning/02-glossary.md`
   Every acronym in plain English. Bookmark this — you'll come back mid-code.

5. **[read]** `docs/learning/03-repo-mental-model.md`
   Every file mapped to a pipeline stage. This is your treasure map for the code sections below.

6. **[skim]** `docs/learning/04-reading-list.md`
   External URLs. Come back later once you know what you don't know.

## Stage 2 — Data model (20 min)

Read these in order. Small files, high leverage. They define the vocabulary the rest of the code speaks.

7. **[read]** `packages/schemas/__init__.py`
   The one-file map of everything the app knows how to represent.

8. **[read]** `packages/schemas/call.py`
   `CallState`, `TranscriptTurn`, `Intent`, `Urgency`, `ExtractedFields`. The heart.

9. **[read]** `packages/schemas/business.py`
   `BusinessProfile`, `BusinessHours`, `ServiceOffering`. What a client's business looks like in JSON.

10. **[read]** `packages/schemas/booking.py`
    `Booking`, `BookingStatus`. The output of a successful call.

11. **[read]** `packages/schemas/tools.py`
    `ToolDefinition`, `ToolCall`, `ToolResult`. How the LLM calls into your code.

12. **[read]** `sample-data/clinic/business.json`
    A concrete BusinessProfile in real JSON. Now the schemas above have weight.

## Stage 3 — Provider adapters (30 min)

The "swap any cloud service for any other" layer. Read the interfaces first, then one concrete example of each.

13. **[read]** `apps/api/app/providers/base.py`
    The abstract classes every adapter implements: `LLMProvider`, `STTProvider`, `TTSProvider`, `TransportProvider`.

14. **[read]** `apps/api/app/providers/factory.py`
    The env-var dispatch. Every `LLM_PROVIDER=xxx` in `.env` maps to one class here.

15. **[read]** `apps/api/app/providers/llm/openai_llm.py`
    A concrete LLM adapter. All the others (Groq, Anthropic, Gemini, Ollama) follow the same shape.

16. **[skim]** `apps/api/app/providers/llm/groq_llm.py`
    Nearly identical to OpenAI — Groq is OpenAI-compatible on purpose.

17. **[skim]** `apps/api/app/providers/llm/anthropic_llm.py`
    Different message shape (`system` split out, `tool_use` blocks). Read to see how the interface hides this.

18. **[skim]** `apps/api/app/providers/llm/gemini_llm.py`, `apps/api/app/providers/llm/ollama_llm.py`
    Optional. Same interface, different SDK shapes.

19. **[read]** `apps/api/app/providers/stt/deepgram_stt.py`
    One concrete STT. Read the docstring and `.transcribe()`.

20. **[skim]** `apps/api/app/providers/stt/groq_stt.py`, `apps/api/app/providers/stt/openai_stt.py`, `apps/api/app/providers/stt/local_whisper_stt.py`
    Same shape, different backends.

21. **[read]** `apps/api/app/providers/tts/elevenlabs_tts.py`
    One concrete TTS.

22. **[read]** `apps/api/app/providers/tts/qwen3_tts.py`
    Local TTS. Read the docstring at the top — it explains preset vs cloning mode.

23. **[skim]** `apps/api/app/providers/tts/openai_tts.py`, `apps/api/app/providers/tts/deepgram_tts.py`, `apps/api/app/providers/tts/cartesia_tts.py`, `apps/api/app/providers/tts/local_piper_tts.py`, `apps/api/app/providers/tts/browser_tts.py`
    Same interface. `browser_tts.py` is a sentinel provider — read it to see how "no cloud TTS available" is handled.

24. **[skim]** `apps/api/app/providers/transport/browser_webrtc.py`, `apps/api/app/providers/transport/vapi_webhook.py`, `apps/api/app/providers/transport/twilio.py`, `apps/api/app/providers/transport/livekit.py`
    Mostly stubs — the actual transport lives in `routes/` for now.

## Stage 4 — The brain (30 min) ⭐

This is the product. Everything else is glue. Read carefully.

25. **[read]** `packages/core_agent/__init__.py`
    Three exports. Confirms scope.

26. **[read]** `packages/core_agent/prompt.py`
    How a `BusinessProfile` becomes the system prompt the LLM sees.

27. **[read]** `packages/core_agent/brain.py` ⭐
    **The most important file in the whole repo.** ~150 lines. The tool-calling loop:
    LLM call → tool call → LLM call → reply. Read line by line.

28. **[read]** `packages/core_agent/extractor.py`
    A second, small LLM call after each turn to distill the transcript into structured fields (name, phone, intent, lead score...). This is what gets written to CRMs.

## Stage 5 — Tools per vertical (30 min)

Each vertical is a set of tool definitions + a handler that runs them.

29. **[read]** `packages/integrations/fake_calendar.py`
    JSON-file backed calendar. Same interface as `GoogleCalendar`.

30. **[read]** `packages/integrations/calendar_factory.py`
    `CALENDAR_BACKEND=fake|google` dispatch.

31. **[read]** `packages/integrations/clinic_tools.py`
    The clinic vertical. `build_clinic_tools()` defines what the LLM can call, `ClinicToolHandler.__call__` runs them.

32. **[read]** `packages/integrations/restaurant_tools.py`
    Same shape, different tools (`book_reservation`, party size).

33. **[read]** `packages/integrations/real_estate_tools.py`
    Adds `qualify_lead` with heuristic scoring. Different output shape (lead, not booking).

34. **[read]** `packages/integrations/vertical_tools.py`
    Factory that picks the right tool set per `business.vertical`.

35. **[read]** `sample-data/restaurant/business.json`, `sample-data/real-estate/business.json`
    Concrete profiles for the new verticals.

## Stage 6 — CRM sinks (15 min)

Where the results go after a call ends.

36. **[read]** `packages/integrations/sinks.py`
    `NoopSink`, `CompositeSink`, `GHLSink`, `SheetsSink`. The `build_sink_from_env` dispatcher.

37. **[read]** `packages/integrations/ghl_client.py`
    Concrete GoHighLevel API client. Read the docstring; skim the methods.

38. **[skim]** `packages/integrations/google_calendar.py`, `packages/integrations/google_sheets.py`
    Real Google adapters. Same interface as `FakeCalendar` for the calendar side.

## Stage 7 — HTTP + WebSocket routes (45 min)

The FastAPI endpoints. Every transport ends up here. Read in this order.

39. **[read]** `apps/api/app/core/config.py`
    Every env var the app understands, in one file. Reference this whenever a doc mentions a variable.

40. **[read]** `apps/api/app/db/session.py`, `apps/api/app/db/models.py`
    SQLite tables: `SessionRow`, `TranscriptRow`, `BookingRow`.

41. **[read]** `apps/api/app/core/session_manager.py` ⭐
    Second most important file. In-memory session state + SQLite mirror. Wires the brain to every transport.

42. **[read]** `apps/api/app/main.py`
    App startup, router registration, static-file mount for the browser sim. Every route is registered here.

43. **[read]** `apps/api/app/routes/chat.py`
    The simplest transport: `/chat/start`, `/chat/turn`, `/chat/end`. This is what the browser sim talks to.

44. **[read]** `apps/api/app/routes/voice.py`
    `/voice/stt` and `/voice/tts`. Browser sim helpers.

45. **[read]** `apps/api/app/routes/sessions.py`
    `/sessions` for dashboard/debug queries.

46. **[read]** `apps/api/app/routes/vapi.py`
    OpenAI-compatible custom-LLM endpoint. Also works for Retell and Bland unchanged.

47. **[read]** `apps/api/app/routes/elevenlabs_compat.py`
    `/v1/text-to-speech/*`, `/v1/voices` — makes any 11L SDK client work with our server.

48. **[read]** `apps/api/app/routes/twilio.py` ⭐
    Real phone calls. µ-law WebSocket protocol, silence detection, audio conversion. Read the file docstring first.

49. **[read]** `apps/api/app/routes/channels.py`
    Delegates to the channel package (below).

## Stage 8 — Messaging channels (20 min)

WhatsApp and Telegram. Both share one pipeline.

50. **[read]** `packages/channels/__init__.py`, `packages/channels/base.py`
    The `Channel` interface and `IncomingMessage` dataclass.

51. **[read]** `packages/channels/pipeline.py`
    The shared `VoiceMessagePipeline`: STT → brain → TTS → send. Every channel reuses this.

52. **[read]** `packages/channels/whatsapp.py`
    WhatsApp Business Cloud API. Read the docstring for setup, then `parse_webhook` and `send_voice`.

53. **[read]** `packages/channels/telegram.py`
    Telegram bot API. Simpler than WhatsApp. Read `parse_webhook` and `send_voice`.

## Stage 9 — Browser call simulator (15 min)

Small, self-contained. No build step.

54. **[read]** `apps/call-simulator/index.html`
    The DOM structure.

55. **[read]** `apps/call-simulator/app.js`
    Mic capture (`MediaRecorder`), POST to backend, play back audio or fall back to `speechSynthesis`.

56. **[skim]** `apps/call-simulator/style.css`
    Cosmetic.

## Stage 10 — Tests (20 min)

Read the tests last. By this point they'll read as executable documentation.

57. **[read]** `apps/api/tests/test_brain_booking_flow.py`
    A full clinic booking flow with a scripted LLM. This is the reference for how the brain behaves.

58. **[read]** `apps/api/tests/test_verticals.py`
    Restaurant + real-estate happy paths, lead scoring, unknown-vertical fallback.

59. **[read]** `apps/api/tests/test_channels.py`
    WhatsApp/Telegram parsing + end-to-end pipeline.

60. **[read]** `apps/api/tests/test_elevenlabs_compat.py`, `apps/api/tests/test_vapi_webhook.py`, `apps/api/tests/test_sinks.py`
    Each covers one route or one integration.

## Stage 11 — Setup docs (read one at a time, when you actually need it)

Don't read all of these in advance. Read the one for the demo you're about to record.

61. **[do]** `docs/runbooks/telegram-first-demo.md` ⭐
    Your first shipped demo. 15 minutes, $0. Do this before anything else in this stage.

62. **[ref]** `docs/twilio-setup.md`
    Real phone calls. Do this second.

63. **[ref]** `docs/vapi-setup.md`
    Hosted phone platform. Matches many Upwork job posts.

64. **[ref]** `docs/channels-setup.md`
    WhatsApp production setup (goes deeper than the Telegram runbook).

65. **[ref]** `docs/qwen3-tts-setup.md`
    Local voice model details, hardware guidance, MPS float16 gotcha.

66. **[ref]** `docs/elevenlabs-setup.md`
    Real 11L account + voice picking.

67. **[ref]** `docs/elevenlabs-compat.md`
    How to point 11L SDKs at our server.

68. **[ref]** `docs/ghl-setup.md`
    GoHighLevel Private Integration token + scopes.

69. **[ref]** `docs/google-setup.md`
    Service account, Calendar + Sheets sharing.

## Stage 12 — Business docs (30 min, read once when planning)

70. **[read]** `docs/architecture.md`
    Layered architecture view. Phases 1-5 roadmap.

71. **[read]** `docs/provider-matrix.md`
    Pick a stack per client. Cost vs quality trade-offs.

72. **[read]** `docs/pricing-notes.md`
    Free tiers, cost-per-call math, what to charge on Upwork.

73. **[read]** `docs/demo-script.md`
    The Loom recording playbook.

74. **[read]** `docs/proposal-snippets.md`
    Reusable paragraphs for Upwork replies.

## Stage 13 — External reading (spread over weeks)

Come back to `docs/learning/04-reading-list.md`. Now that you've read the code, the outside blog posts will click.

Order suggested:
- LiveKit "State of voice agents 2026"
- Deepgram "Voice agent latency guide"
- Twilio Media Streams docs (skim while looking at `routes/twilio.py`)
- Vapi custom-LLM docs (skim while looking at `routes/vapi.py`)
- Kokoro-82M and Qwen3-TTS model cards
- LiveKit Agents + Pipecat docs (only when you plan to self-host)

Skip everything not on that curated list.

---

## The three ⭐ files you must understand deeply

If time is short, read only these three plus the schemas above:

1. `packages/core_agent/brain.py` — the tool-calling loop
2. `apps/api/app/core/session_manager.py` — where state lives
3. `apps/api/app/routes/twilio.py` — the hardest transport (audio codec + silence detection); once you get this, WhatsApp/Telegram/browser are trivial

## When you finish

You'll be able to:
- Add a new voice provider (~30 min)
- Add a new LLM provider (~30 min)
- Add a new vertical (dentist, gym, law firm) with its own tools (~1 hour)
- Add a new channel (Discord, Slack) (~2 hours)
- Deploy the whole thing to a VPS + real phone number (~half a day)

That's the full product surface. From here, everything is just applying these five moves in new combinations.
