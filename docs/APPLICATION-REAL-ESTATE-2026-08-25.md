# Application — European Real-Estate Voice Agent

**Draft for your review before submission. Rewrite in your own voice; the structure + technical content is the deliverable.**

**Attachments to include with submission:**
1. Best call recording (Flow 1 from `DEMO-SCRIPT-REAL-ESTATE-2026-08-25.md`)
2. Second call recording (Flow 4 — angry caller / warm transfer)
3. Architecture diagram (networking's artifact `0e82270c-4ec3-4af3-93f0-6f6b226c4009` — publish public link)
4. GitHub link: `github.com/SyedAbbas-CM/ai-voice-receptionist` (or private access if you'd rather)

---

## Cover paragraph (keep short, personal)

Hi — I'm Syed. I build voice agents for a living and I've been running a custom Twilio + Deepgram + OpenAI + ElevenLabs stack in production over the last four months. I've dialed in on the exact problems you list — GDPR-aware call recording, structured qualification instead of open-ended chat, CRM writes that survive partial failures, warm transfer, and honest fallback when the agent doesn't know something. I've attached two live demo recordings against a Lisbon real-estate persona ("Ribeira Prime") built on the same architecture I'd propose for you. Both recordings, the architecture diagram, and the GitHub repo are linked above.

I'll answer your four questions directly.

---

## 1) Voice AI agent example — with demo

The system I'm attaching is a multi-tenant voice receptionist I built from scratch on Twilio Media Streams. The stack is:

- **Twilio** for telephony (EU numbers via Twilio Ireland)
- **Deepgram Flux** for streaming STT (EU endpoint)
- **OpenAI Chat Completions** for reasoning (routed via OpenAI Ireland)
- **ElevenLabs Flash v2.5** for TTS (US-hosted with signed DPA)
- **FastAPI + SQLAlchemy + SQLite → PostgreSQL** for state and tenant isolation
- **HubSpot EU** as the primary CRM adapter (Pipedrive and GoHighLevel supported with the same sink pattern)
- **Deployed on AWS Lightsail** (currently us-east-1 for latency reasons I'll explain in Q4; would migrate to eu-west-1 or eu-central-1 for your project)

**What's in the demo recordings:**

- **Recording 1 (happy path):** UK-based buyer, €800k budget, 3-month timeline, non-resident. The agent qualifies him, checks calendar availability, reads back exactly the slots the calendar returned, books a viewing, and confirms. HubSpot contact + note created live. SMS confirmation to the caller. Owner email with an ICS attachment. All fully autonomous.

- **Recording 2 (edge case — angry caller / warm transfer):** Caller is furious about a missed viewing. Persona rule fires immediately — the agent acknowledges briefly, does not try to salvage or upsell, and warm-transfers to a human with an urgency flag. This is the failure mode where a lot of voice bots make it worse. Handling it correctly is more valuable than handling ten happy paths.

**Why I built this instead of using Vapi or Retell:** exactly the reasons in Q2.

---

## 2) Which voice AI platform I would recommend — and why

**My honest answer: it depends on your team, your call volume, and whether you value speed-to-launch or long-term control more than cost.**

**For your project specifically, I would recommend a custom system on Twilio + Deepgram + OpenAI + ElevenLabs (or Vapi's Elliot voice if you value voice quality above unit economics).** I would not recommend a pure Vapi or Retell hosted-platform deployment for a European real-estate agency at meaningful call volume. Here's the breakdown.

**Vapi (well-engineered, mature product):**
- Strong turn-taking / interruption handling out of the box
- Native primitives for blind transfer, warm transfer, DTMF, end-call phrase detection, voicemail detection
- Elliot V2 voice (powered by xAI Grok TTS) is the most human-sounding TTS I've tested — measurably above ElevenLabs Flash v2.5 on Vapi's own Humanness Index (88/100 vs 68/100)
- $0.05/min platform fee ON TOP of STT/LLM/TTS/telephony costs. At 10,000 call minutes/month that's a $500/mo tax you can't offset
- Elliot voice is only accessible inside Vapi's orchestration — you can't use it standalone

**Retell:**
- Wins on perceived naturalness in blind listening tests, mostly through latency tuning and turn-detection quality
- Similar hosted-platform economics — you're paying their runtime, not just their voice
- Similar vendor-lock-in on custom logic — "custom LLM" hook exists but sits behind their orchestrator

**Custom (what I recommend for you):**
- No per-minute platform tax — you pay only the underlying model/telephony providers
- Full control over turn-taking, tool schemas, prompt versioning, and GDPR data flow
- Same TTS options — you can still use Deepgram + OpenAI + ElevenLabs directly. If you want the Grok TTS voice quality without Vapi's fee, xAI now exposes Grok TTS directly at $15/M chars vs ElevenLabs Flash's $50/M chars
- You own the recordings and transcripts — nothing sits in a third-party platform's storage layer

**The tradeoff is engineering time to first stable production call.** A Vapi assistant can be live in a day. A custom system took me around 8 weeks to get to the reliability level in the attached recordings. If your call volume will be low (say under 500 min/mo for the first year) and you value time-to-market, start with Vapi. If you expect any real volume, or if EU data residency + audit trail integrity matter more than a fast setup, go custom.

**One recommendation regardless of platform:** don't optimize for the flashiest voice at the expense of turn-taking. A slightly less "wow" voice that never talks over the caller and hangs up cleanly beats a beautiful voice that steps on people. Turn-taking is the single biggest predictor of whether a caller feels the agent is competent.

---

## 3) How I approach edge cases

You asked about four. Each has been exercised in the attached demo (`docs/DEMO-SCRIPT-REAL-ESTATE-2026-08-25.md`).

**An unqualified lead** — the agent doesn't force qualification. If the caller says "just browsing" or is vague about intent, the agent clarifies gently ("we do sales, rentals, and valuations — anything specific?") and if the answer is still vague, it takes a light-touch message ("want me to text you our current shortlist?") rather than pushing. Nothing is worse than a bot that grills a curious person until they hang up frustrated. The lead is still captured — just with an appropriate score and no wasted human follow-up time.

**An angry or confused caller** — persona-level rule: acknowledge the specific problem briefly, do NOT chirp "Sure!" or "No problem!", do NOT try to salvage the interaction into a booking. `human_transfer_rules.always_transfer_on: ["complaint", ...]` fires an immediate warm transfer with urgency flagged. If the human isn't available, we take a message with priority="urgent" and the caller is told a specific time-frame (not "someone will call you back" — that's meaningless — but "Maria will call you within the hour"). For confused callers we slow the agent down and offer to send materials by SMS/WhatsApp; recovery via a callback is often better than trying to explain everything on the phone.

**A caller asking something outside the agent's knowledge** — the persona rule is explicit: "you say honestly, 'that's not something I can answer accurately, but I can have Maria or João call you back within the hour and they'll be much better on that.' You do not bluff." No hallucination, no dead air. The knowledge gap is logged (KnowledgeGap surface — dashboard shows all unanswered questions so the business owner can answer once and improve the knowledge base for the next caller). This is a competitive advantage over both custom-system self-builds AND hosted platforms — most agents either hallucinate or say "I don't know" without a path forward.

**Failure of the CRM or calendar integration during the call** — the caller must not hear about it. Sinks are wrapped in isolation: a HubSpot 429 doesn't fail the calendar write; a calendar outage doesn't fail the SMS follow-up. HubSpot / GHL / calendar clients have 4-attempt exponential backoff with jitter, honoring Retry-After. Persistent failures write to an outbox table so the failed operation retries automatically once the integration recovers (no data loss). Local SQLite is the source of truth for bookings; CRM is a downstream sink. If HubSpot is down for an hour we still have the booking, we still text the caller, and the sync catches up when HubSpot comes back. This is well-tested — I have 14 tests specifically pinning the retry policy against 429/503/network-error scenarios.

---

## 4) Highest technical risk in production

**Turn-taking under adversarial acoustic conditions.**

Every other risk on this project — CRM writes, calendar conflicts, GDPR compliance, cost overrun — has a well-understood engineering pattern and someone has solved it before. Turn-taking has patterns too, but every acoustic environment is different (a caller on a car speakerphone in a Lisbon tunnel is a different problem than a caller on a landline in a quiet office), and the failure mode is caller-visible in a way the caller will describe as "the bot sucks" without being able to articulate what specifically went wrong.

Sub-risks in decreasing severity:

**a. Barge-in false positives.** Caller coughs, agent thinks they interrupted, stops mid-sentence. Caller now has to re-ask. Fixed by two-stage barge-in classification (VAD → semantic acoustic classifier) but calibration is per-language and requires real-caller data.

**b. End-of-turn false negatives.** Caller finishes speaking, agent doesn't know it's their turn, dead air. Fixed by combining Deepgram Flux's semantic EOT with a fallback timer, but the timer value is a tradeoff — too short and you interrupt slow speakers; too long and the call feels laggy.

**c. Structured-slot capture during dictation.** Caller reads a phone number "0-4-9-1-2-3-4-5-6-7-8" over 8 seconds with pauses. If the STT emits a `final` too eagerly, the agent thinks the caller finished at digit 4. Handled by explicit slot-capture mode that routes STT directly to a validator instead of the LLM (bypasses eager EOT) — but this needs to be wired PER expected input type.

**d. Interaction between hangup logic and long TTS tails.** Booking-confirmation replies can be 15+ seconds of audio. If the hangup timer fires before TTS completes, caller hears the tail cut mid-word. Fixed by polling for `SPEAKING` → `LISTENING` state transition before the Twilio REST hangup, with a bounded max-wait.

**All four have caller-visible failure modes and none have "the right constant" you can set once and forget.** Every acoustic environment shifts the tradeoffs. The right answer is production observability that flags each class specifically (I emit `POST_EOT_HOLD_MS` per turn, `BARGE_IN_CONFIRMED` vs `BARGE_IN_BACKCHANNEL`, `FAREWELL_HANGUP_TTS_DRAINED` vs `FAREWELL_HANGUP_TTS_TIMEOUT`) so I can bisect the acoustic distribution my system is currently mis-classifying and calibrate against it.

For your project specifically, I'd want two weeks of pilot-tenant call data before I'd claim any of the four are "solved" — the calibration is empirical, not theoretical.

**Secondary risk:** GDPR data flow across sub-processors. Your DPO will ask for a signed DPA from every provider that touches PHI-adjacent data (name + phone + service context = quasi-identifier). I've done this due-diligence chain already — Twilio Ireland has EU DPA, Deepgram has EU endpoint + DPA, OpenAI Ireland has EU processing + Business Associate Agreement pattern, HubSpot EU has data residency. ElevenLabs is US-hosted with a signed DPA — that would be my one flag to your DPO for explicit sign-off, and it's why I mentioned considering Grok TTS (also US-hosted but different DPA terms). Alternatives include Deepgram TTS (EU) if voice quality is acceptable.

---

## Budget + timeline

I'd propose the project in three phases so you can validate before committing to the full scope:

**Phase 1 — Discovery + pilot deployment (2 weeks, €4,000–€6,000)**
- Requirements walkthrough with your team
- Fixture built for your business — real hours, real services, real FAQs, real transfer rules, real CRM
- Deployed to your EU region on Lightsail (eu-west-1 or eu-central-1)
- Live pilot with one number, one agent, up to 100 calls to work out real-caller edge cases
- GDPR sub-processor DPA chain documented for your DPO

**Phase 2 — Production integration (3 weeks, €6,000–€10,000)**
- CRM adapter to your specific instance (HubSpot / Pipedrive / GHL)
- Calendar integration (Google Calendar or Calendly)
- SMS/email follow-up wired to your templates
- Post-call n8n webhook if you want the automation-workflow flexibility (I emit a canonical BookingEvent + CallEndEvent shape you can consume in n8n)
- Warm-transfer + take-message wired to your on-call agent rotation
- Handover documentation + operator training

**Phase 3 — Iterate on real calls (ongoing, €1,500–€3,000/month retainer, optional)**
- Weekly review of call transcripts flagged by the dashboard
- Prompt + policy tuning based on actual caller behavior
- Knowledge base updates as new questions surface
- Latency + reliability tuning

**Total for Phase 1 + 2:** €10,000–€16,000 over ~5 weeks, delivering a production-ready system integrated into your CRM + calendar. Ongoing is optional but valuable — the biggest wins come from real-caller data.

Happy to scope tighter if you have a specific ceiling. I'd rather propose honestly than come back mid-project asking for more.

---

## Availability

I can start on Discovery within 3 business days of contract sign-off. I'm currently based in Karachi (UTC+5), which overlaps well with Lisbon's business hours (UTC+0/+1). Async-friendly, but I run live-call debugs together with the client during the pilot so I'd want 2–3 scheduled calls per week during Phase 1.

Repo, architecture diagram, and demo recordings all linked above. Happy to walk through the code + system design on a call if that would help — no commitment.

Thanks for reading this far.

— Syed
