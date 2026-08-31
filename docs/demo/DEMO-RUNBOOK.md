# Demo runbook — how to record a receptionist call for a client

**Time to prepare:** 5 min.
**Time to record:** ~2 min of actual call + 1 min of dashboard walkthrough.
**Deliverable:** one MP4 file, one URL if the client wants deeper inspection.

## What you're demoing

**One product, three angles in one video:**
1. **Voice agent answers the call** — sounds natural, understands the caller
2. **Post-call annotator + trace** — you own the reviewer experience, not a black box
3. **Same call replayed with structured events** — every decision the agent made, in timeline form

Client sees: "this thing actually works AND I can inspect what it did." That's the wedge.

---

## The 5-min prep

### 1. Get your Mac ready to record

- **QuickTime Player** → File → **New Screen Recording**
- Click the small arrow next to the red record button → set **Microphone** to your built-in mic
- Set **Quality: Maximum**
- Do NOT click Record yet

### 2. Get your phone ready

- Have the Twilio number saved as a contact called "Smile Dental Demo"
- Put phone on **speakerphone** (loud enough your Mac mic can hear both sides)
- Silence notifications so nothing beeps mid-call

### 3. Get your browser tabs ready

Open these tabs in order, so during the recording you swipe left-to-right:

1. **Blank tab** — start here, hides everything
2. **`https://agent.eternalconquests.com/admin/annotate`** — sign in advance (already logged in? good)
3. **`https://agent.eternalconquests.com/trace/{latest CallSid}`** — you'll grab this AFTER the call
4. **The GitHub repo** (optional, if client wants to see code)

**Login credentials for the dashboard:**
```
URL:      https://agent.eternalconquests.com/admin/login
Password: XdEO3BRosytHZeiBvoPB
```
(Rotate this before real client demo — it's currently in this repo's memory.)

### 4. Rehearse the call script once (dry run, don't record)

Pick ONE of these 3 scripts based on how long you want the demo.

---

## Call scripts — pick one

### 📞 SHORT (30 seconds) — booking a cleaning

```
Agent:  "Thanks for calling Smile Dental Clinic, how can I help?"
You:    "I want to book a cleaning next Wednesday morning."
Agent:  [asks for preferences → checks availability → offers slots]
You:    "Nine thirty works."
Agent:  [asks for phone number]
You:    "Five five five, one two three, four five six seven."
Agent:  [confirms + books]
You:    "Thanks!"
```

Shows: booking, calendar integration, phone capture.

### 📞 MEDIUM (60 seconds) — Christiaan-style follow-up

```
Agent:  "Thanks for calling Smile Dental Clinic, how can I help?"
You:    "I want to book a follow-up."
Agent:  "Follow-up to what?"        [← discovery drill fires]
You:    "A cleaning I had last week."
Agent:  "What day works?"
You:    "Any afternoon next week."
Agent:  [checks availability → offers slots]
You:    "Two PM Tuesday sounds good."
Agent:  "What's the best number to reach you?"
You:    "Five five five, one two three, four five six seven."
Agent:  [confirms + books]
You:    "Thanks!"
```

Shows: discovery drill (asking clarifying questions before booking), calendar sync, phone capture. This is our best-differentiated flow.

### 📞 EDGE (20 seconds) — FAQ handling, no booking

```
Agent:  "Thanks for calling Smile Dental Clinic, how can I help?"
You:    "Do you take Delta Dental insurance?"
Agent:  [answers from business profile]
You:    "Great, thanks."
Agent:  [closes]
```

Shows: agent doesn't force a booking, knows the business's actual info.

---

## During the recording (~2 min total)

1. **Start screen recording** on QuickTime — pick the whole screen
2. **Say to camera** (or just to Loom): "This is our AI receptionist for a dental clinic. I'm calling the number now."
3. **Call the Twilio number** from your phone
4. **Speak the script** you rehearsed
5. **After the call ends**, immediately open the **annotator tab** and refresh the index
6. **Click the call at the top** — walk through the transcript for 30 seconds:
   - "Here's the full turn-by-turn — caller in blue, agent in tan, tool calls in grey"
   - "The reviewer can tag any turn that went wrong — I'll flag one as a great response"
   - Click any turn → tag it `great_response` in the right panel → save
7. **Click the "humanness ↗" link** to open the trace view
8. **Say**: "This is the trace — every decision the agent made, timestamped, so we can debug or improve it."
9. **Stop recording**

### Total video: ~2:30

---

## After you record

1. **QuickTime** → File → **Save** → name it `smile-dental-demo-{YYYY-MM-DD}.mov`
2. Optionally convert to MP4:
   ```
   ffmpeg -i smile-dental-demo-2026-08-31.mov -c:v libx264 -c:a aac smile-dental-demo-2026-08-31.mp4
   ```
3. **Send to client:**
   - The MP4 file (upload to WeTransfer, Google Drive, or Loom's own hosting)
   - A one-paragraph email: what they just saw + why it matters
   - Offer: "If you want to try it live, here's the number [+1-…]. Anyone can call it."

---

## Recording quality tips

- **Speakerphone** — your Mac mic needs to hear both sides. Test volume with a 10-second recording first.
- **Quiet room** — background noise ruins voice quality on the caller side too (STT catches everything).
- **Slow down** — the agent handles normal speed but for demos, speak slightly slower + clearer so on the recording it sounds crisp.
- **Don't say numbers as digits** — say "five five five, one two three, four five six seven" not "5551234567" — the agent handles both but slower speech records better.
- **First greeting is fast** — the agent starts talking within ~500ms. If you cut in too early, you'll clip its greeting.

---

## What if the call goes badly on camera?

**Real risk.** The agent has bugs. If it happens during the demo, you have two options:

### Option A — cut + re-record

- QuickTime lets you delete and re-record without any hard commitment. Just stop, retry, no client sees it.

### Option B — own the bug on camera, use it as a selling point

If the agent messes up mid-call, calmly:
- "Interesting — see what happened? We caught it, it's in the trace. Let me show you."
- Open the trace/annotator, point to the specific issue
- "This is why we have the reviewer console. Every call becomes training data, so the next call is better."

Clients respect this MORE than a perfect demo. It says "we know how to improve this system," which is the actual product.

---

## Alternative: Loom

Loom's free tier gives you unlimited recordings up to 5 minutes. Same process, but:
- Automatic upload to a shareable link
- Built-in camera bubble (your face in corner) — clients like this
- Auto-transcription of your narration (searchable)
- Analytics: who watched, for how long

Trade-off: needs to install their browser extension. If sending to freelance clients on Upwork, WeTransfer link to an MP4 is friction-free.

---

## Sending to a client — email template

> Hi [name],
>
> I built the AI voice receptionist we discussed. Short video attached ({XX} seconds): I call the number, the agent takes a booking, then I show you the reviewer console.
>
> Two things worth noting:
> 1. This runs on our infrastructure — we could deploy a copy on yours or leave it on ours, your call.
> 2. Every call becomes reviewable + annotatable, so the agent gets better with use. Not a black box.
>
> If you want to try it, call [+1-…] — anyone can call. Happy to run a short pilot with your real number.
>
> [Your name]

---

## What NOT to show

- **The 5-bug list from your last test call.** Client doesn't need to know it took 3 tries to fix the PII redactor. Show the fixed version.
- **The annotation dashboard mid-annotation** — filter to a WIN call first, then click through. Failure calls are for internal use.
- **The trace page's raw JSON** — the humanness projection is clean; raw events are engineer-only.
- **Github repo** unless client explicitly asks.
- **Cost per call** — leads to bad conversations. Say "usage-based pricing, we'll scope it to your call volume" if asked.

---

## After the demo — what to close on

Pick ONE of these based on the client:

- "Want to see it work on YOUR business's info? Send me your services + hours and I'll swap it in — 15 minutes."
- "Want a per-tenant instance you own? We can spin one up in your AWS."
- "Want to run this in parallel with your current answering service for a week and compare?"

All three give the client a small next commitment. That's the goal.
