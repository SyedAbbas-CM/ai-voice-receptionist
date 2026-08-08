# Browser Widget Test Script

Say these lines slowly and clearly. Wait ~3s between turns so the TurnManager can finalize.

## Setup

```bash
# 1. Server should be running
curl -sf http://127.0.0.1:8000/health && echo " up"

# 2. Watch the server log while you test (open a second terminal)
tail -f /tmp/uvicorn.log

# 3. Open widget
open http://127.0.0.1:8000/call-stream/
```

Click **Call**, allow mic. Wait for the greeting to finish before you speak.

## Test 1 — hours (FAQ lookup)

**You:** "What are your hours today?"
**Expected:** something like "We're open until 5 PM today" (matches Monday-Wednesday `07:30-17:00` in profile)
**Fails if:** generic "call during business hours" or "I don't know"

## Test 2 — location

**You:** "Where are you located?"
**Expected:** "2847 Coit Road, Suite 210, Plano" (or close)
**Fails if:** doesn't mention Coit / Plano

## Test 3 — service list

**You:** "What services do you offer?"
**Expected:** should name several — cleanings, exams, whitening, Invisalign, etc.
**Fails if:** says "we do dental services" (generic) or lists < 3 things

## Test 4 — doctor lookup

**You:** "Who does Invisalign there?"
**Expected:** "Doctor Ramanathan handles our Invisalign consultations"
**Fails if:** "I'd need to check" or invents a doctor name

## Test 5 — booking flow (multi-turn)

**You:** "I'd like to book a cleaning."
**Expected:** asks for name, phone, preferred day/time

**You:** "My name is Alex, phone 555-1234."
**Expected:** confirms name + phone, asks day/time

**You:** "How about next Thursday afternoon?"
**Expected:** offers a slot, e.g. "how about Thursday at 2 PM?"

**You:** "That works."
**Expected:** confirms booking

**Fails if:** loses track of previous turns, asks the same thing twice, forgets your name

## Test 6 — cancellation policy

**You:** "What's your cancellation policy?"
**Expected:** "24 hours notice, same-day may incur a $25 fee"

## Test 7 — barge-in / interruption

Start it saying something long: **"Tell me about all the services you offer."**
While it's speaking, cut in with: **"Actually, I just want a cleaning."**
**Expected:** stops mid-sentence, acknowledges the interruption
**Fails if:** keeps talking over you, or forgets you interrupted

## Test 8 — insurance

**You:** "Do you take Delta Dental?"
**Expected:** "Yes, we're in-network with Delta Dental PPO"

## Test 9 — emergency escalation (safety)

**You:** "I have a tooth abscess and it's really painful."
**Expected:** empathetic tone, offers same-day emergency slot, or escalation phone
**Fails if:** treats it like a routine booking

## Test 10 — out-of-scope

**You:** "What's the weather?"
**Expected:** politely redirects back to dental
**Fails if:** actually tries to answer

---

## After the call

```bash
# Get the last call ID from server log
CID=$(grep "actor twilio start\|browser_" /tmp/uvicorn.log | tail -1 | grep -oE "browser_[a-f0-9]+")
echo "call: twilio_$CID"

# See the full event timeline (should show STT, kernel state, LLM, tools, TTS)
curl -s "http://127.0.0.1:8000/debug/call/twilio_$CID/timeline" | python3 -m json.tool

# See the recent errors
curl -s "http://127.0.0.1:8000/debug/errors/recent?hours=1" | python3 -m json.tool
```
