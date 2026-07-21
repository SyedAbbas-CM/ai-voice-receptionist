# Upwork proposal templates

Three ready-to-paste templates for the three most common voice-agent job types on Upwork. Replace bracketed placeholders. Keep them short — Upwork proposals under 200 words convert best.

**Before you send any of these**, you need one Loom URL. Follow `docs/runbooks/vapi-setup.md` to get a phone number and record a 60-second demo. Every proposal below assumes you paste that Loom into `[LOOM_URL]`.

---

## Template 1 — Inbound AI receptionist (clinic / dental / spa / vet / small business)

**When to use**: job posts asking for "AI receptionist", "answer calls", "book appointments", "24/7 phone answering", "AI phone assistant".

**Anchor price**: $1,500-3,500 setup + $150-400/mo maintenance.

```
Hi [CLIENT_NAME],

I've built exactly this — an AI receptionist that answers calls, checks
your calendar, and books appointments. Short Loom (60s) so you can hear
it and skip the sales talk: [LOOM_URL]

What you'd get:
- Live phone number that answers 24/7 in the voice you pick
- Real calendar integration (Google Calendar, Cal.com, or your existing
  booking system — tell me which)
- FAQ handling from a simple JSON of your business info
- Emergency-line escalation to a human when needed
- Post-call summary emailed to you or logged to a spreadsheet

Delivery: 3-5 days for the working phone number, another 2 days to
integrate your specific calendar/CRM.

Pricing: $2,500 flat for the build + $250/mo for the number, cloud costs,
and ongoing tweaks. Cheaper long-term than a $30/hr receptionist and
never sick.

Happy to jump on a 10-minute call. What's your booking software?

— [YOUR_NAME]
```

---

## Template 2 — Outbound cold-caller / lead qualifier (real estate, insurance, home services)

**When to use**: job posts asking for "AI cold caller", "outbound dialer", "AI lead qualifier", "call leads from spreadsheet", "SubtoDealz-style", "seller financing calls".

**Anchor price**: $2,000-5,000 setup + $500-1,000/mo retainer.

```
Hi [CLIENT_NAME],

I ship this exact system. AI voice cold-calls leads from a Google Sheet,
qualifies them, and writes the disposition back — HOT_LEAD, COLD_LEAD,
CALLBACK_REQUESTED, etc. Loom of the flow: [LOOM_URL]

What you'd get:
- Google Sheet you drop leads into → AI dials in business hours only
- Configurable business hours, cooldown between attempts, DNC list
- Post-call GPT-4 classifies the transcript, writes back to the sheet
  automatically (no 8-minute waits, no manual review)
- HOT_LEAD → your phone gets a text; COLD/DNC → auto-marked done
- Full TCPA-compliant: consent lookup, AI disclosure on first line,
  business hours enforced in the callee's timezone

I already replaced the exact n8n workflow you might be using today with
a clean FastAPI backend — happy to show you the mapping.

Pricing: $3,000 flat + $500/mo (covers the phone number, Vapi credits
at scale, and me tuning the prompt to your script).

Timeline: 5-7 days to your first paid demo call.

What script are you using now? Send it and I'll pre-tune it.

— [YOUR_NAME]
```

---

## Template 3 — WhatsApp / Telegram voice-note bot (concierge, DM automation, agencies)

**When to use**: job posts asking for "WhatsApp bot", "WhatsApp voice assistant", "AI concierge", "auto-reply to voice notes", "Telegram bot for our sales team".

**Anchor price**: $1,000-2,500 flat + $150-300/mo.

```
Hi [CLIENT_NAME],

I already have this running. Customer sends a voice note on WhatsApp,
AI transcribes it, replies with either a text or a voice note back in
your brand voice. Loom: [LOOM_URL]

What you'd get:
- Your existing WhatsApp Business number connected (or Meta gives you
  one free with 1000 conversations/mo)
- Voice-note-in, voice-note-out with sub-3-second latency
- Business profile in a JSON I hand you — FAQs, hours, booking rules
- Same code also works on Telegram if you want that channel too
- Full conversation history in a dashboard I can give you access to

Delivery: 3 days to a working sandbox, 1-2 more for your specific WhatsApp
Business API onboarding (Meta paperwork takes them ~24 hours).

Pricing: $1,800 flat + $200/mo. Free tier from Meta covers most small
businesses under 1000 conversations/mo.

What are the top 5 questions your customers usually ask? I'll pre-tune
the FAQ answers before we start.

— [YOUR_NAME]
```

---

## Universal follow-up (send after 48 hours if no reply)

Copy-paste this if the client doesn't respond in ~2 days:

```
Hi [CLIENT_NAME], just following up. If Loom + timeline don't fit, tell
me what part doesn't and I'll adjust. Also happy to just answer questions
without the pitch — no cost, no commitment.
```

---

## What NOT to say in proposals

- **Don't mention 8 LLM providers, 7 TTS providers, provider-swappable everything.** Clients don't care. Pick the specific ones that fit their stack and only mention those.
- **Don't say "I built a whole framework."** Clients want a shipped result, not a research project.
- **Don't promise sub-500ms latency on your Mac.** You can't hit it locally. Only promise it on Vapi + cloud stack.
- **Don't quote hourly rates.** Anchor a flat fee. Clients who want hourly-rate contractors are the wrong clients.

## What to lead with in the Loom

60 seconds max:
1. **First 5 seconds**: "This is what a call sounds like." Then just play the call. No intro, no title card.
2. **Next 40 seconds**: The call runs. Caller asks, AI answers, tool call fires, booking confirmed.
3. **Last 15 seconds**: Show the sheet/CRM row that got written. Say "same stack works on your calendar / your CRM / your voice."

Never explain what "provider-swappable" means. Never show code. Show the working thing.

## Job types to prioritize on Upwork

Search these exact strings, sorted by "Most Recent":

- "AI receptionist" — Template 1
- "AI phone answering" — Template 1
- "Vapi assistant" — Template 1 or 2 depending on job details
- "AI cold caller" — Template 2
- "real estate AI outbound" — Template 2
- "WhatsApp AI bot" — Template 3
- "voice AI agent" — read the post, pick 1/2/3

## Job types to skip

- "Build me a voice AI from scratch" without a use case → they don't know what they want; wasted proposal
- Budget under $500 → not enough to cover phone number + credits + your time
- "No AI hallucinations" as a requirement → they don't understand LLMs; expensive support burden
- Clients with < 4.5 star ratings on Upwork → learn the hard way why they got those ratings
