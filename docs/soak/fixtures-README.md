# SOAK replay fixtures

Recorded caller-side audio (µ-law 8kHz WAV) used by
`apps/api/scripts/replay-audio.py` to drive deterministic regression
scenarios through the actor's Twilio WS path.

**These WAVs are NOT checked in** — they can be several MB each and
some contain real caller voices.  Store them locally at:

    apps/api/data/soak_fixtures/*.wav

Each fixture is one caller turn (silence-padded on both ends).  The
replay script sends them at real-time cadence so the actor's VAD,
STT bridge, and turn manager behave as in a real call.

## Required fixtures

| Filename                          | Scenario | Caller says                                           |
|-----------------------------------|----------|-------------------------------------------------------|
| `s1-fake-wait-hook.wav`           | 1        | "Hi, uh, I have a question about your services"       |
| `s2-barge-then-reask.wav`         | 2        | "What times work for tomorrow?" ... "Actually wait. What others are available?" |
| `s3-phone-pk-spoken.wav`          | 3        | "Book me. Zero, triple three, five two four four, seven seven two." |
| `s4-phone-mixed-dtmf.wav`         | 4        | Voice: "zero three three three" then DTMF handled by replay flag |
| `s5-ani-accept.wav`               | 5        | "Book me for tomorrow. Use the number I'm calling from." |
| `s6-possible-phone.wav`           | 6        | "Book me. Phone is 555 123 4567."                     |
| `s7-silence-mid-capture.wav`      | 7        | "Zero three three three." (then 10s silence baked in) |
| `s8-benign-question.wav`          | 8        | "Tell me about your practice."                        |

## Recording guidance

1. Record on a real phone in µ-law 8kHz (Twilio's native format).  A
   16kHz PCM recording is also fine — the replay script resamples
   automatically (see `_wav_to_mulaw`).
2. Trim aggressive lead-in silence (>500ms) to keep runs snappy.
3. For scenario 7, splice ~10s of true digital silence AFTER the
   spoken part so the stall watchdog can fire.
4. For scenario 4 (DTMF), the WAV only carries the voice portion;
   DTMF injection is a separate replay flag (TBD in v2 of replay-audio.py).

## Running

Whole suite:

    python apps/api/scripts/replay-audio.py \
        --ws ws://localhost:8000/twilio/stream \
        --fixture-dir apps/api/data/soak_fixtures/ \
        --report /tmp/soak-$(date +%Y%m%d-%H%M).json

Then feed each CallSid from the report to `verify-call.sh`:

    jq -r '.[].call_sid' /tmp/soak-*.json | while read sid; do
        apps/api/scripts/verify-call.sh "$sid"
    done
