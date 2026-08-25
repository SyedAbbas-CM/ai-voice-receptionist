# Production Research Report: Human-Like, High-Speed LLM Voice Agents with Deepgram, Groq/OpenAI and ElevenLabs

## Executive summary

Your problem is **not primarily an ElevenLabs problem and not primarily a “better personality prompt” problem**. It is a pipeline problem with four interdependent layers:

**turn detection → LLM first useful text → TTS first useful audio → conversational behaviour.**

Your current architecture is fundamentally sound for a production receptionist:

**Twilio → Deepgram Nova-3 → routed LLM → ElevenLabs Flash v2.5 → Twilio.**

I would **keep that chained architecture**. OpenAI itself distinguishes speech-to-speech Realtime agents from chained voice pipelines and notes that the chained approach remains appropriate when you need transcripts, deterministic business logic, structured tool use and control over each stage—all of which apply strongly to your receptionist. citeturn16view2 ElevenLabs Flash v2.5 is also specifically positioned for real-time workloads, with model inference around 75 ms under favourable conditions. citeturn23search1turn23search4

The existing call evidence shows the right diagnosis: callers describe the agent as robotic and lacking warmth, while the LLM stage dominates perceived latency. Your brief reports roughly 1.53 seconds median end-of-user-speech to first agent audio, with approximately 350 ms STT, 1.9 seconds LLM first-token on observed turns and roughly 300 ms TTS first-byte. Those component figures cannot all belong to the exact same median turn because they exceed the reported end-to-end median, so the first instrumentation fix should be **one monotonic per-turn waterfall rather than separately aggregated timing samples**. fileciteturn0file1

There is also an important repository-state discrepancy. The later implementation notes say native Groq streaming, shared HTTP clients and OpenAI Fast service tier have already been shipped, with an internal benchmark dropping the full-prompt OpenAI TTFT from about 1,534 ms to 772 ms. fileciteturn0file0 However, the ZIP snapshot supplied for this research still contains the earlier architecture: `GroqLLM` has no true `stream_complete()`, Groq creates fresh `httpx.AsyncClient` instances, the router therefore buffers Groq replies, and the ElevenLabs WebSocket is opened separately for each sentence. **Before benchmarking anything else, establish which commit is actually deployed.**

My recommended target architecture is:

```mermaid
flowchart LR
    U[Caller / Twilio] --> DG[Deepgram streaming STT]
    DG --> TM[Turn manager + endpointing]

    TM --> DS[DialogueState]
    DS --> FP{Deterministic fast path?}

    FP -->|Yes| TXT[Safe response template]
    FP -->|No| LR[LLM router]

    LR --> G20[Groq GPT-OSS 20B fast path]
    LR --> OA[OpenAI fast/reliable path]
    LR --> G120[Groq GPT-OSS 120B complex path]

    G20 --> SS[Native SSE token stream]
    OA --> SS
    G120 --> SS

    SS --> SB[Sentence / phrase buffer]
    TXT --> SB

    SB --> EW[One ElevenLabs WS per assistant turn]
    EW -->|complete sentences| EL[Flash v2.5]
    EL --> TW[Twilio audio]

    U -. barge-in .-> CANCEL[Cancel generation + drain stale audio]
    CANCEL -.-> LR
    CANCEL -.-> EW
    CANCEL -.-> TW

    DS --> TOOLS[Business tools]
    LR <--> TOOLS
    TOOLS --> DS
```

The three highest-impact things to ship first are:

| Priority | Change | Why it matters | Expected effect |
|---|---|---|---|
| **First** | Make **true LLM streaming and persistent connections non-negotiable**: native Groq SSE, shared `httpx.AsyncClient`, current GPT-OSS models, OpenAI Fast where supported | Removes avoidable transport/buffering before TTS can even start | Largest pure-latency improvement |
| **Second** | Replace the conversational-policy layer: **10–30-word turns, specific acknowledgements, one question at a time, contextual backchannels, mood/pace mirroring, rare fillers** | This directly attacks “robotic receptionist template” behaviour | Largest humanness improvement |
| **Third** | Change ElevenLabs from **one WS per sentence to one WS per assistant turn**, feed complete sentences, and A/B Talia/Chelsea with lower stability and `style=0` | Removes repeated connection/prosody resets while improving voice fit | Combined latency + naturalness improvement |

The goal should **not** be to make the bot constantly say “umm”, “uh”, “you know”. Research has repeatedly found that indiscriminate fillers do not automatically make task-oriented agents seem more human and can make them appear less competent or less likeable. Contextually appropriate fillers and backchannels are much more useful. citeturn9search0turn9search2turn9search1

Likewise, **do not optimise only TTFT**. For voice, the useful metric is:

> **caller end-of-turn → first semantically useful agent audio**

A model generating `"Sure!"` in 250 ms is not necessarily better than one producing `"Gotcha — you're looking for a cleaning."` in 400 ms. The latter gives ElevenLabs useful prosodic context and gives the caller evidence that the agent understood them.

The production objective I recommend is:

| Metric | First milestone | Production target |
|---|---:|---:|
| End-of-user-speech → first audible agent speech | p50 ≤ 900 ms | **p50 ≤ 700 ms** |
| Same metric, tail | p95 ≤ 1,500 ms | **p95 ≤ 1,200 ms** |
| Primary LLM TTFT | p50 ≤ 500 ms | **p50 ≤ 350–450 ms** |
| First useful text chunk ready | ≤ 1 complete sentence | **6–14 useful spoken words** |
| Ordinary agent response length | ≤ 40 words | **10–30 words** |
| Barge-in → old audio stopped | p95 ≤ 300 ms | **p95 ≤ 250 ms** |
| Tool/business correctness | no regression | **no regression** |
| Booking/confirmation correctness | no regression | **no regression** |

Those are engineering SLOs, not claims that human conversation normally pauses for 700 ms. Human turn transitions are considerably tighter: cross-linguistic work finds conversation systematically minimises gaps and overlap, while spoken-dialogue research commonly cites human response starts around a few hundred milliseconds and notes that systems waiting roughly 700–1,000 ms merely to decide a speaker has stopped can feel sluggish. citeturn10search0turn8search11turn8search0 A chained STT→LLM→TTS system cannot literally reproduce a 200-ms human gap every turn; it should instead minimise avoidable delay and use safe, context-aware early speech where appropriate.

## Latency root causes and the architecture that should replace them

### Where the time is going

The voice latency equation should be instrumented as:

\[
T_{\text{first audio}}
=
T_{\text{endpoint}}
+
T_{\text{LLM connect/queue}}
+
T_{\text{LLM TTFT}}
+
T_{\text{usable chunk}}
+
T_{\text{TTS connect}}
+
T_{\text{TTS TTFB}}
+
T_{\text{playback}}
\]

Crucially, some stages can overlap. That is why summing independently reported p50 values gives misleading results.

Your current research brief identifies the LLM as the major bottleneck, and your later notes report an internal OpenAI benchmark of approximately **1,534 ms → 772 ms** after enabling Fast service tier. fileciteturn0file0 OpenAI now positions Fast processing as its latency-optimised API mode, with substantially higher processing speed than standard service on supported model/account combinations. citeturn16view0

The supplied ZIP snapshot, however, reveals several additional avoidable delays.

| Layer | Repository observation | Why it hurts | Fix |
|---|---|---|---|
| Groq LLM | ZIP's `GroqLLM` only exposes buffered completion | Router cannot get tokens until Groq finishes | Native SSE `stream_complete()` |
| Groq HTTP | Fresh `AsyncClient` in calls/fallbacks in ZIP | Repeated DNS/TCP/TLS/connection setup can add latency | Application-lifetime HTTP client |
| Groq model IDs | Old Llama defaults remain in ZIP | Free/developer Groq availability changed | Move to GPT-OSS IDs |
| OpenAI | Proper SSE exists in ZIP | Good baseline | Keep; A/B Fast and persistent Responses WS |
| LLM output | `max_tokens=300` in the voice path | Encourages verbose replies and long tool loops | Separate speech/tool budgets |
| LLM→TTS | Sentence buffering exists | Correct idea | Keep complete-sentence boundary |
| ElevenLabs | New TTS WebSocket for each sentence | Repeated handshake + fresh synthesis context | One WebSocket per assistant turn |
| Turn-taking | Turn/endpoint configuration varies by deployment | Late endpoint detection can consume hundreds of ms before LLM begins | Trace endpointing explicitly |
| Prompt | System prompt is large | More prefill work; cache-prefix design matters | Trim style text and optimise stable prefix later |

OpenAI's own latency guidance makes two points particularly relevant here: **smaller/faster models normally lower latency, and output length is one of the most reliable latency levers**. Its guidance notes that cutting generated tokens can approximately cut generation time proportionally, and explicitly recommends concise output instructions and hard generation bounds. citeturn14view3

### True Groq streaming is mandatory

Groq supports streaming Chat Completions; the API returns incremental deltas rather than requiring the complete assistant response first. citeturn5search3 Your router already knows how to consume a streaming-provider contract, so Groq should implement exactly the same semantic interface as your OpenAI adapter.

At minimum, the implementation needs the following shape:

```python
# groq_llm.py
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx


class GroqLLM:
    _client: httpx.AsyncClient | None = None

    @classmethod
    def client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=30.0,
                    write=5.0,
                    pool=1.0,
                ),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=40,
                    keepalive_expiry=60.0,
                ),
            )
        return cls._client

    async def stream_complete(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 96,
    ) -> AsyncIterator[dict]:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
        }

        if self.model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with self.client().stream(
            "POST",
            f"{self.base_url}/openai/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue

                raw = line[5:].strip()
                if raw == "[DONE]":
                    yield {"type": "done", "is_final": True}
                    return

                event = json.loads(raw)
                delta = event["choices"][0].get("delta", {})

                text = delta.get("content")
                if text:
                    yield {
                        "type": "text",
                        "text": text,
                        "is_final": False,
                    }

                # Aggregate streamed tool-call deltas here rather than
                # ever forwarding their JSON to the speech layer.
                if delta.get("tool_calls"):
                    yield {
                        "type": "tool_call_delta",
                        "tool_calls": delta["tool_calls"],
                        "is_final": False,
                    }
```

The exact event shape should match your existing `RouterLLM`/OpenAI contract; the important point is that **tool-call deltas must be aggregated separately and must never enter TTS**.

The later Claude notes say this change has already been made in another repository state. fileciteturn0file0 Therefore, do not blindly re-implement it. Add a startup assertion/integration test proving that the deployed `GroqLLM.stream_complete` is actually overridden, then benchmark the deployed commit.

### Reuse network connections everywhere

An `AsyncClient` should be owned at process/provider lifetime, not constructed for every conversational request. HTTP keep-alive avoids repeatedly establishing transport connections and gives HTTP/2 a chance to reuse connections efficiently.

Your OpenAI implementation already follows this pattern in the ZIP; Groq and its fallback helpers should do the same.

The application's shutdown hook should explicitly close the clients:

```python
@classmethod
async def close_client(cls) -> None:
    if cls._client is not None:
        await cls._client.aclose()
        cls._client = None
```

Instrument `connection_reused=true/false` rather than assuming keep-alive is working.

### Replace the stale Groq models

This is now time-sensitive. Groq shut down `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` for Free and Developer tier usage on **16 August 2026**. Groq recommends `openai/gpt-oss-20b` as the 8B replacement and `openai/gpt-oss-120b` or Qwen 3.6 27B as replacements for the 70B model. citeturn23search0

That means these should become the initial routing candidates:

```text
Simple conversational / FAQ / slot gathering:
    Groq openai/gpt-oss-20b
             ↓ failure / uncertainty / complex tool decision
    OpenAI fast model
             ↓ optional high-complexity route
    Groq openai/gpt-oss-120b or stronger OpenAI model
```

Groq currently lists GPT-OSS 20B at roughly **1,000 generated tokens/sec**, $0.075/M input and $0.30/M output, with a 131,072-token context window; GPT-OSS 120B is listed around **500 tokens/sec**, $0.15/M input and $0.60/M output, with the same context family. Those provider throughput numbers are not TTFT guarantees—the scheduler, prompt prefill, rate limits and network path still matter—but they make both models compelling benchmark candidates. citeturn0search0turn0search1

GPT-OSS supports configurable reasoning effort on Groq. For a receptionist turn such as “Do you have anything Tuesday afternoon?”, start at `reasoning_effort="low"`; do not spend reasoning latency on routine slot collection unless your eval shows material tool-selection regressions. citeturn6search4turn6search0

### Do not retry slowly inside the live voice path

A live caller should not experience:

```text
Groq 429
    → sleep 500 ms
    → retry
    → sleep 1 s
    → retry
    → OpenAI
```

Use a **latency-bounded fallback policy**:

```python
VOICE_PROVIDER_DEADLINE_MS = 650
RATE_LIMIT_FAILOVER_MS = 50

try:
    return await groq_stream(...)
except RateLimitError:
    return await openai_stream(...)  # immediately
```

Long exponential retries are appropriate for background work. They are usually wrong for a synchronous voice turn.

This matters especially on low Groq service tiers because rate limits can be reached before model inference becomes the bottleneck. Production benchmarks therefore need to distinguish **provider/model latency** from **rate-limit behaviour**.

### OpenAI: keep SSE first, then test persistent Responses WebSocket

The current OpenAI SSE design is fundamentally correct: the Responses and Chat APIs support incremental streaming so the application can process output before completion. citeturn14view5

OpenAI's Responses API also offers a persistent WebSocket mode with incremental request context and `previous_response_id`. OpenAI specifically highlights benefits for workflows with repeated tool calls and documents warm-up support without generation. citeturn16view3

That makes the existing experimental persistent-WS code worth benchmarking, but **do not assume it will save 500–800 ms on every receptionist turn**. The biggest documented gains are associated with longer multi-step/tool-heavy interactions. A simple “What time do you close?” turn may benefit much less. citeturn16view3

Run this A/B:

| OpenAI transport | Prompt | Connection | What to measure |
|---|---|---|---|
| Chat/Responses SSE | identical | warm HTTP/2 | TTFT baseline |
| Chat/Responses SSE + Fast | identical | warm HTTP/2 | Fast-tier gain |
| Responses WebSocket | identical | persistent | normal turn gain |
| Responses WebSocket | identical | persistent + warm-up | first-call gain |
| Responses WebSocket | identical | persistent | tool-loop total latency |

Your existing internal benchmark reportedly measured approximately **772 ms TTFT for OpenAI Fast vs 1,534 ms standard** on the real prompt. Treat that as encouraging internal evidence, not as a universal OpenAI performance guarantee. fileciteturn0file0

### Prompt caching matters, but it is not the first pure-speed fix

Both OpenAI and Groq can reuse common prompt prefixes. Groq currently supports automatic prompt caching on GPT-OSS and says cached prefixes can reduce latency as well as input-token charges; its cache expires after two hours without use. citeturn23search5 OpenAI likewise bases prompt caching on shared prompt prefixes and recommends placing static content before dynamic content where possible. citeturn14view0turn14view1turn21view0

Your large system prompt makes this useful, but it should come **after true streaming and connection reuse**, because a cold caller still needs a fast path.

There is one important architectural implication: frequently changing date/time/session text near the beginning of the prompt can reduce cross-request exact-prefix reuse. Because your current date/time section is explicitly load-bearing, do **not** reorder it casually. First add a prompt snapshot regression test; only then test whether a stable policy prefix plus later dynamic context preserves behaviour while increasing cache hits.

Also correct one point from the earlier speed research: **Predicted Outputs is not a good way to pre-seed “Sure!” or “Got it!”**. OpenAI describes Predicted Outputs for cases where a substantial portion of the response is already known, such as regenerating a document with limited changes. citeturn16view1 When you already know the entire safe reply, a deterministic fast path is simpler and faster than asking an LLM to predict your prediction.

### Output length should be treated as an architectural setting

Your current `max_tokens=300` is excessive for an ordinary receptionist speaking turn.

Do not replace it with a single tiny global cap, because function arguments and confirmations can legitimately require more space. Split the budgets:

```python
VOICE_REPLY_MAX_TOKENS = 96
TOOL_DECISION_MAX_TOKENS = 192
TOOL_FINAL_REPLY_MAX_TOKENS = 96
SAFETY_REPLY_MAX_TOKENS = 160
```

Then constrain ordinary spoken style at the prompt level to **10–30 words**, usually one or two sentences.

OpenAI's latency guidance explicitly recommends reducing generated tokens because generation length is a large component of latency. citeturn14view3 For voice, this also improves turn-taking: a caller cannot interrupt sentence five if the assistant never unnecessarily generates sentence five.

A hard text postprocessor should be a **last resort**, not the main brevity mechanism. Never mechanically truncate:

- emergency instructions;
- dates, times or phone numbers;
- a booking confirmation;
- legally/compliance-relevant wording;
- a completed tool result.

### The model portfolio I would benchmark

Prices and specifications below reflect provider documentation available at the research date and should be rechecked before procurement. Provider tokens-per-second figures are not directly comparable with measured caller-facing TTFT. citeturn0search0turn0search1turn11search0turn11search5turn11search10turn1search15

| Model | Provider | Approx. context | Indicative API price per M input/output tokens | Role I would test | Main trade-off |
|---|---|---:|---:|---|---|
| **GPT-OSS 20B** | Groq | 131k | **$0.075 / $0.30** | Primary fast path | Extremely attractive speed/cost; evaluate tool accuracy and rate limits |
| **GPT-OSS 120B** | Groq | 131k | **$0.15 / $0.60** | Harder turns / fallback | Better capacity; about half 20B's advertised throughput |
| **gpt-4o-mini** | OpenAI | 128k | about **$0.15 / $0.60** | Existing control | Known integration and cheap; your normal TTFT has been slower |
| **GPT-4.1 mini** | OpenAI | ~1M | about **$0.40 / $1.60** | Tool-use benchmark | Large context, non-reasoning, but more expensive than 4o-mini |
| **GPT-5.4 nano** | OpenAI | 400k | about **$0.20 / $1.25** | New fast-front-line candidate | Designed for inexpensive high-volume tasks; measure voice quality rather than assuming |
| **GPT-5.4 mini** | OpenAI | 400k | about **$0.75 / $4.50** | Strong OpenAI fallback | Higher quality/capacity at higher cost |

Do not route by “largest model wins”. Route by **task complexity**:

```text
hours / location / service FAQ / slot collection
    → GPT-OSS 20B or fastest validated OpenAI small model

ambiguous request / multi-intent / correction / frustration
    → validated stronger model

tool failure / safety / emergency / policy-sensitive
    → deterministic business rules first,
      model only for natural-language rendering where appropriate
```

That is faster and safer than making a 120B-class model reason through “What time do you close?”

## Human-like dialogue, prompt engineering and conversational context

### Humanness is mostly timing, acknowledgement and contingency

A human-feeling receptionist does not need to fool callers into believing she is literally human. What callers perceive as conversational naturalness comes from behavioural signals:

- she understood the specific thing they just said;
- she responds at an appropriate moment;
- her answer is proportional to the question;
- she does not ask three questions simultaneously;
- she adapts to irritation, uncertainty and hurry;
- she can be interrupted;
- she repairs misunderstandings naturally;
- she does not repeat canned phrases.

Turn timing is particularly important. Human conversation is organised around tight transitions between speakers, and dialogue research finds that context-sensitive response timing improves perceived naturalness. citeturn10search0turn8search0 Backchannels such as “yeah”, “right”, “oh” and short acknowledgements are meaningful because they signal attention and understanding without taking over the conversational floor. citeturn8search1

This is why the SubtoDealz prompt is directionally stronger than the existing clinic output: it uses short turns, contractions, active listening and energy matching rather than corporate reception templates. Your brief already identifies those behaviours as the successful internal reference. fileciteturn0file1

### Do not solve this by spraying fillers everywhere

There is useful research against the naïve approach. A CHI study of conversational fillers found that adding fillers to task-oriented agent speech did not reliably make the agent seem more human and could lower perceived intelligence or likeability. Other work finds **contextualised** fillers can improve perceived responsiveness or appropriateness, particularly when the system actually needs time. citeturn9search0turn9search2turn9search1

So the policy should be:

> **Backchannels are common enough to signal listening. Disfluencies are rare enough to remain meaningful.**

Recommended frequencies are implementation heuristics to A/B test, not universal psycholinguistic constants:

| Device | Production policy |
|---|---|
| “Gotcha”, “okay”, “right”, “yeah” | Use when they acknowledge actual caller information |
| “Perfect” | Use after receiving a useful slot/detail, not every turn |
| “That makes sense” | Only where the caller actually gave reasoning/context |
| “Hmm” | Rarely, when considering ambiguity |
| “Well…” | Rarely, when naturally introducing a qualification |
| “Um/uh” | Almost never in normal receptionist operation |
| “You know”, “kind of”, “sort of” | Avoid unless it adds actual interpersonal meaning |
| Filler during emergency/compliance | Never |
| Filler while reading dates/numbers | Never |
| Filler during final booking confirmation | Never |

### Recommended PERSONA text

This replaces only the persona field and leaves the load-bearing sections untouched:

```text
You’re Alex, the front-desk lead at Smile Dental Clinic in Plano.

You’ve been here long enough to know the doctors, the schedule rhythms, and the way regular patients tend to call. You sound like a capable thirty-something Texan talking to one person — warm, easy, attentive, and genuinely glad to help, never like you’re reading receptionist copy.

You naturally use contractions. “I’m”, “we’ve”, “that’s”, “you’re”, and “I’ll” should sound more normal than formal alternatives.

Your little conversational habits are things like “gotcha”, “okay”, “yeah”, “perfect”, and occasionally “y’all” when it genuinely fits. They are not catchphrases. Don’t force them and don’t repeat the same one every turn.

When someone is in a hurry, you get concise and move. When they’re relaxed or chatty, you can be a little more conversational. When they’re nervous, hurting, confused, or upset, the upbeat energy comes down immediately: you get calm, clear, and useful.

You don’t perform empathy with customer-service lines. You show that you listened by responding to the specific thing they said and taking the next useful step.

You never sound salesy, over-cheerful, overly apologetic, or polished like a call-centre script. You don’t praise every answer with “Great!” or “Perfect!”. You don’t stack multiple questions when one will do.

You enjoy helping the clinic’s patients, but competence comes before personality.
```

This operationalises the user's existing Alex/Texas-friendly concept while moving personality from autobiographical brochure text into **observable speaking behaviour**. fileciteturn0file1

### Recommended HOW YOU ACTUALLY TALK text

I would replace the style section with this exact text:

```text
## HOW YOU ACTUALLY TALK

You are speaking on a phone call, not writing a chat response.

Most turns are 10–30 spoken words. Usually one or two short sentences. Say enough to move the conversation forward, then give the caller the floor.

YOUR DEFAULT TURN SHAPE:
1. Briefly show that you caught what they said, when an acknowledgment is useful.
2. Give the answer or take the next useful action.
3. Ask at most ONE question unless two details absolutely belong together.

ACKNOWLEDGE THE SPECIFIC THING — NOT EVERY TURN:
Use natural reactions like “Gotcha,” “Okay,” “Yeah,” “Right,” “Absolutely,” or “Perfect” when they actually fit.
Vary them. Sometimes the most natural response has no acknowledgment at all.
Never begin every answer with “Of course!”, “Absolutely!”, “Great!”, or “Perfect!”.

USE CONTRACTIONS BY DEFAULT:
Say “I’m”, “you’re”, “we’ve”, “that’s”, “it’s”, “I’ll”, “we can”, and “don’t”.
Formal uncontracted speech should be unusual unless clarity requires it.

LISTEN BEFORE YOU MOVE ON:
If the caller gives you a name, preference, concern, correction, or reason, react to THAT before asking for the next field.
Bad: “What service and date would you like?”
Better: “Gotcha — you’re looking for a cleaning. Is there a day that works best?”

ONE QUESTION AT A TIME:
Do not interrogate the caller with several slot questions in one sentence.
Collect the next missing detail, then stop and let them answer.

MIRROR THEIR PACE:
If they’re brief or sound busy, become brief.
If they’re relaxed and conversational, you can loosen up a little.
If they’re formal, stay politely professional.
If they’re casual, you can be casual too.
Never imitate an accent, slang, anger, profanity, or agitation just because the caller uses it.

WHEN THEY’RE UPSET:
Drop the cheerful receptionist energy. Don’t say canned lines like “I understand your frustration.”
Name the practical issue when useful and start fixing it.
Example: “Yeah, I can see why that’d be frustrating. Let me check what happened with that appointment.”

WHEN THEY’RE NERVOUS, HURTING, OR DESCRIBING AN EMERGENCY:
Become calm, slow, direct, and reassuring.
No filler words. No jokes. No chirpy “Perfect!” or “Awesome!”.
Follow the existing emergency, safety, and escalation rules exactly.

SMALL DISFLUENCIES ARE RARE:
A natural “hmm” or “well” is okay when you are genuinely working through ambiguity.
Do not sprinkle “um”, “uh”, “you know”, “kind of”, or “sort of” into normal answers just to sound human.
Never use disfluencies in booking confirmations, compliance language, emergency guidance, phone numbers, dates, times, prices, or other precision-critical information.

IF YOU DIDN’T CATCH SOMETHING, REPAIR IT LIKE A PERSON:
Do not pretend you understood.
Say exactly what you did catch when helpful.
Example: “Sorry — I caught ‘Oliver’, but I missed the bit before that. What were you looking to schedule?”

DON’T OVER-EXPLAIN:
Give the caller the part they need now. Additional detail can come after they ask for it.

DON’T REPEAT YOURSELF:
Avoid recycling the same acknowledgment, question, greeting, apology, or farewell.

ENDINGS ARE ONE-AND-DONE:
When the caller clearly says goodbye, give one warm closing and finish. Do not keep generating new goodbye variations.

PUNCTUATION IS FOR SPEECH:
Write sentences that sound good aloud. Prefer simple punctuation and short sentences.
Avoid corporate headings, numbered explanations, semicolon-heavy prose, excessive exclamation marks, emojis, and chat-style formatting.

FRONT-LOAD USEFUL MEANING:
The first spoken sentence should contain something useful, not just filler.
“Gotcha — you’re trying to book a cleaning” is better than “Of course! I’d be happy to assist you with that.”

TOOL TRUTH BEATS CONVERSATIONAL STYLE:
Never imply that you checked, booked, changed, cancelled, confirmed, or found something until the existing tool and confirmation rules allow you to say so.
All existing tool-call, wait-promise, booking, date/time, hallucination, phone-number, compliance, and safety rules remain higher priority than this speaking style.
```

This design deliberately keeps natural-language behaviour separate from your load-bearing business rules. That separation is important: conversation style should be replaceable without weakening booking confirmation, hallucination prevention or tool truth. The user's brief explicitly identifies those rules as previously regression-tested and load-bearing. fileciteturn0file1

### Few-shot examples that teach the behaviour

**Frustrated caller**

```text
Caller: This is the second time I’ve called. Somebody moved my appointment and nobody told me.

Agent: Yeah, I can see why that’d be frustrating. Let me pull up what happened. What name is the appointment under?
```

Notice that the agent does not respond with a generic “I sincerely apologise for the inconvenience.”

**Quick booking**

```text
Caller: I just need a cleaning sometime Tuesday afternoon.

Agent: Gotcha — Tuesday afternoon for a cleaning. Is earlier in the afternoon better, or closer to the end of the day?
```

After the tool returns:

```text
Agent: I’ve got 2:30 or 4:00 available Tuesday. Which one works better?
```

**Caller trails off or STT misses part**

```text
Caller: I’m trying to see... Oliver... uh...

Agent: Sorry — I caught “Oliver”, but I missed what you wanted to see us for. What are you looking to schedule?
```

That is much more human than confidently converting an uncertain fragment into a booking slot.

**Emergency**

```text
Caller: My face is swelling up and it’s getting harder to breathe.

Agent: Okay — trouble breathing needs urgent medical attention. Please call emergency services now. Once you’re safe, we can help with the dental side.
```

The exact medical/emergency wording should continue to come from your existing safety policy; this example illustrates **tone**, not a replacement clinical protocol.

**Compliment / small talk**

```text
Caller: You guys were really nice last time, by the way.

Agent: Aw, I’m glad to hear that. I’ll pass that along. So, are we getting you back in for another cleaning?
```

The few-shots should remain compact. Large banks of near-duplicate examples inflate prompt prefill and can make the model mimic one phrase obsessively.

### Context engineering: stop treating conversation history as the state machine

The LLM should not have to infer the complete operational state from an ever-growing transcript every turn.

Maintain structured state beside the transcript:

```python
@dataclass
class DialogueState:
    intent: str | None
    phase: str

    slots: dict[str, object]
    confirmed_slots: set[str]

    caller_mood: str          # neutral, rushed, chatty, upset, anxious
    caller_pace: str          # terse, normal, verbose

    last_agent_act: str | None
    last_user_act: str | None

    pending_tool: str | None
    tool_status: str | None
    tool_result: dict | None

    needs_repair: bool
    interruption_count: int
    end_call: bool

    generation_id: int
    latency_budget_ms: int
```

A normal booking turn can then give the LLM:

```text
STATE
Intent: book_appointment
Service: cleaning
Preferred day: Tuesday
Preferred period: afternoon
Missing: exact slot
Caller mood: neutral
Caller pace: brief
Tool status: no availability lookup yet

LAST TURNS
Caller: I just need a cleaning Tuesday afternoon.
Agent: Gotcha — Tuesday afternoon for a cleaning. Is earlier or later better?
Caller: Later's better.
```

That is superior to forcing the model to reread dozens of raw turns looking for every commitment.

Keep the full transcript for audit/history, but progressively compress older conversation into:

- facts;
- caller preferences;
- commitments;
- completed tool effects;
- unresolved questions;
- safety-relevant information.

Do **not** summarise away completed business actions such as “appointment booked at 4:00”. Those belong in durable structured state.

### Business logic should sometimes bypass the LLM completely

OpenAI's own latency guidance recommends not defaulting to a model when deterministic logic can answer the problem directly. citeturn14view3turn14view4

Examples:

```text
"What time do you close?"
    → trusted business-data template

"What's your address?"
    → trusted business-data template

"Can you repeat that?"
    → replay/rephrase last safe spoken answer

"Bye"
    → deterministic one-line closing

Tool returns exactly one available slot
    → deterministic natural-language slot template

LLM/provider outage
    → "Sorry — I'm having trouble pulling that up right now.
       I can take your details or have the office follow up."
```

These paths can reach TTS in tens of milliseconds of application work rather than spending several hundred milliseconds deciding how to say a known fact.

The crucial restriction is that templates must draw from **trusted business state**, never invent availability or imply a tool succeeded before it did.

## ElevenLabs voice, prosody and TTS streaming

### Keep Flash v2.5

I agree with the constraint.

ElevenLabs describes Flash v2.5 as its real-time low-latency model, with approximately 75 ms model inference under favourable conditions. citeturn23search1turn23search4 The complete caller-facing TTFB is necessarily higher because network geography, WebSocket setup and audio transport surround model inference. ElevenLabs' published examples for its US infrastructure show roughly 150–200 ms WebSocket TTFB for US clients and substantially larger figures from South Asia. citeturn23search7

Therefore, if your voice orchestration server itself is in Pakistan/South Asia while callers and Twilio traffic are primarily US-based, **infrastructure placement may be costing more than another week of TTS parameter tweaking**. ElevenLabs' own published comparison puts South Asia→US Flash WebSocket TTFB at approximately 380–440 ms versus 150–200 ms in the US. citeturn23search7

That does not mean your timezone matters. It means **measure the physical region of the server actually opening the ElevenLabs connection**.

### Voice candidates

There is an additional reason to re-evaluate Sarah now. ElevenLabs has announced changes to its older Default voice set, including migration/replacement guidance for Sarah ahead of the old defaults' retirement at the end of 2026; Talia is listed as the corresponding warmer replacement. citeturn17search1turn18search1

I would audition these in this order:

| Rank | Voice | Voice ID | Why I would test it | Risk |
|---|---|---|---|---|
| **A** | **Talia — Warm Soft Guide** | `OZ0L6eISlOejga3XjDFt` | Warm, low-pressure direction; also the logical Sarah migration candidate | Could be too soft if settings are overly stable |
| **B** | **Chelsea — Conversational and Bright** | `NHRgOEwqx5WZNClv5sat` | Strong fit for friendly front-desk conversation without necessarily sounding “presenter-like” | Brightness may be too energetic for emergency turns |
| **C** | **Maisie — Friendly Casual Neighbor** | `QtY3JBOUKEB5xzrRfOKc` | Good test for “real person next door” rather than corporate reception | Must verify clinic professionalism |
| **D** | **Jade — Upbeat and Natural** | `g7LVvkPWALzPxOQbF6OE` | Useful alternate natural/upbeat candidate | Highest risk of sounding too cheerful |

The exact IDs for Chelsea, Maisie, Jade and Talia above are drawn from currently indexed public catalogue listings rather than a stable first-party documentation page for each ID. citeturn19search0turn19search1turn19search2turn19search3 **Before committing any voice ID to production, call ElevenLabs' voice-list endpoint in your own account and verify the ID, availability and licence status.** ElevenLabs' API documentation explicitly exposes voice listing/voice-ID retrieval for this purpose. citeturn23search2

Use the *same* audition script for every voice:

```text
Hey, thanks for calling Smile Dental. Yeah, I can help with that.
Are mornings usually easier for you, or would an afternoon appointment be better?

Gotcha — Tuesday afternoon. I've got 2:30 or 4:00 available.
Which one works better?

Okay — if the swelling's getting worse and you're having trouble breathing,
please call emergency services now. Once you're safe, we can help with the dental side.

Aw, I'm glad to hear that. I'll pass that along.
```

This exposes four things a single cheerful demo sentence will hide:

1. ordinary warmth;
2. numbers and booking precision;
3. low-energy emergency delivery;
4. conversational small talk.

Voice Library access/licensing also needs to be checked per voice/account. ElevenLabs describes the Voice Library as a marketplace that includes Professional Voice Clones, and API/library availability varies by plan; free-tier API access to the library is restricted. citeturn23search4turn17search10

A properly consented Professional Voice Clone may produce a more distinctive clinic persona than any stock voice, but I would **not assume it beats stock for this application**. ElevenLabs says default/synthetic/instant-cloned voices can have lower latency than Professional Voice Clones on Flash, so PVC is a quality/persona experiment rather than a free win. citeturn23search7

### Voice settings

The ElevenLabs defaults are already around stability `0.5`, similarity `0.75` and style `0`. ElevenLabs says lower stability creates broader emotional variation while higher stability can become monotonous; it also recommends generally leaving style exaggeration at zero because nonzero style costs compute, may increase latency and can reduce stability. Speaker boost likewise adds computational load and some latency. citeturn23search2turn23search3turn23search11

Therefore, I would **not** set `style=0.5` simply because the voice sounds robotic.

Start here:

```json
{
  "stability": 0.40,
  "similarity_boost": 0.75,
  "style": 0.0,
  "use_speaker_boost": false,
  "speed": 1.0
}
```

Then A/B:

| Profile | Stability | Similarity | Style | Speaker boost | Speed | Purpose |
|---|---:|---:|---:|---|---:|---|
| Current control | 0.50 | 0.75 | 0.00 | current behaviour | 1.00 | Baseline |
| **Recommended warm** | **0.40** | **0.75** | **0.00** | **false** | **1.00** | First production candidate |
| Expressive | 0.32–0.35 | 0.75 | 0.00 | false | 1.00 | Test whether lower stability improves warmth |
| Light-style experiment | 0.40 | 0.75 | 0.05–0.10 | false | 1.00 | Only after latency test |
| Clarity experiment | 0.40 | 0.75 | 0.00 | true | 1.00 | See whether speaker boost survives telephony compression |

These values are **A/B starting points**, not empirically universal optima. ElevenLabs itself says voice generation is non-deterministic and that settings interact with the chosen voice. citeturn23search3

I expect the largest quality improvement to come from **voice selection + better text**, not turning style exaggeration up.

### One ElevenLabs WebSocket per assistant turn

This is one of the strongest code-level recommendations in the report.

The WebSocket API is explicitly designed for incremental streamed text. The connection is initialised once, then additional text messages are sent, and an empty text message finalises generation. citeturn23search8turn23search9

Your architecture should therefore look like:

```text
assistant turn begins
    ↓
open ElevenLabs WS
    ↓
send initial settings
    ↓
LLM emits:
    "Gotcha — you're looking for a cleaning."
    ↓
send sentence on SAME WS
    ↓
LLM emits:
    "Is Tuesday or Wednesday better?"
    ↓
send sentence on SAME WS
    ↓
LLM turn final
    ↓
send EOS {"text": ""}
    ↓
close/release WS
```

Not:

```text
sentence one
    → open WS
    → synthesise
    → EOS
    → close

sentence two
    → open another WS
    → synthesise
    → EOS
    → close
```

Opening a connection for every sentence wastes connection setup and, by inference, deprives the synthesiser of the continuity that its streaming interface is built to maintain. The API explicitly supports repeated text messages on one connection. citeturn23search8turn23search9

A useful abstraction is:

```python
async with elevenlabs.start_turn(
    voice_id=voice_id,
    model_id="eleven_flash_v2_5",
) as tts_turn:
    async for event in llm.stream_complete(...):
        for sentence in sentence_buffer.push(event.text):
            await tts_turn.send_text(sentence + " ")

    for remainder in sentence_buffer.flush():
        await tts_turn.send_text(remainder + " ")
```

Then:

```python
class ElevenLabsTTSTurn:
    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri)
        await self.ws.send(json.dumps({
            "text": " ",
            "voice_settings": self.settings,
        }))
        self.reader_task = asyncio.create_task(self._read_audio())
        return self

    async def send_text(self, text: str) -> None:
        await self.ws.send(json.dumps({"text": text}))

    async def finish(self) -> None:
        await self.ws.send(json.dumps({"text": ""}))

    async def cancel(self) -> None:
        self.reader_task.cancel()
        await self.ws.close()
```

### `auto_mode=true` is good—provided you send complete sentences

ElevenLabs documents an important trade-off: `auto_mode` reduces latency by disabling normal chunk scheduling/buffers, but it is recommended only when the input arrives as **full sentences**. Sending partial sentences can seriously reduce generation quality. citeturn23search7turn23search9

That means you should **not** directly send every streamed LLM token to ElevenLabs:

```text
"Got"
"cha"
" — you"
"'re looking"
...
```

Nor should you send arbitrary 15-character slices.

Your `SentenceBuffer` concept is correct.

I would evolve its first-chunk policy from character-based to semantic/word-based:

```python
FIRST_CHUNK_MIN_WORDS = 6
FIRST_CHUNK_TARGET_WORDS = 8
FIRST_CHUNK_MAX_WORDS = 14
NORMAL_TURN_MAX_WORDS = 30
```

The first chunk should be something like:

> “Gotcha — you're looking for a cleaning.”

not:

> “Gotcha!”

and not:

> “Gotcha — you're looking for a cleaning and you'd preferably like to come sometime Tuesday afternoon but…”

This gives TTS enough semantic context for useful prosody while allowing audio to begin before the full response is generated.

### Prosody should mostly come from ordinary text

ElevenLabs explicitly states that its synthesis models infer emotion from textual context and punctuation; the voice sliders principally affect consistency and resemblance rather than magically creating emotional intent. citeturn23search1turn23search4

Therefore:

| Text device | Recommendation |
|---|---|
| Natural commas | Yes, sparingly |
| Short sentences | **Yes** |
| Contractions | **Yes** |
| `"hmm"` | Rare, contextually |
| `"um"` / `"uh"` | Rare-to-never |
| One exclamation mark | Occasionally |
| `!!!` | No |
| Ellipses `...` | Do not rely on them for deterministic pause timing |
| Em dash `—` | Good natural punctuation, but not a precise timing API |
| ALL CAPS | Do not use as a production prosody control |
| Excess comma insertion | No; can create choppy synthetic rhythm |
| Descriptive instructions such as `[warmly]` | No for Flash text—ordinary descriptive text may itself be spoken |
| SSML break | Use only when a precise pause is genuinely useful |

ElevenLabs warns that descriptive emotional text may itself be spoken, so do not emit things such as:

```text
[gentle and reassuring] Okay, let's get that checked.
```

into normal Flash TTS. citeturn23search1turn23search4

For precise pauses, ElevenLabs supports SSML parsing in its streaming interface when enabled, including SSML-related pronunciation functionality. citeturn23search8 The `<break>` capability is a more deliberate mechanism than accumulating ellipses, but it should be used sparingly because excessive synthetic pauses can themselves sound unnatural.

For example:

```xml
Okay.<break time="200ms" /> I can check Tuesday afternoon.
```

Do not put a `<break>` between every sentence. Natural sentence segmentation already provides a pause.

### Do not add artificial chunk delays

Do not implement:

```python
await tts.send(sentence)
await asyncio.sleep(0.25)
await tts.send(next_sentence)
```

unless a controlled test demonstrates a specific need.

Let punctuation and the synthesiser determine ordinary within-turn timing. Artificial delays enlarge latency and can create the very “agent is thinking in blocks” sensation you are trying to remove.

The correct boundary is:

**LLM token stream → semantic sentence buffer → immediate send to the already-open TTS WebSocket.**

## Dialogue state, interruptions, fallbacks and business safety

### Make the voice agent full-duplex even though the model pipeline is chained

Human conversation allows overlap, backchannels, interruption and rapid turn repair. Research on full-duplex dialogue systems treats barge-in and simultaneous listening/speaking as essential parts of realistic spoken interaction rather than edge cases. citeturn8search2turn8academia15

Your system therefore needs **generation ownership**.

Every assistant turn gets a monotonically increasing generation ID:

```python
state.generation_id += 1
my_generation = state.generation_id
```

Before forwarding any new audio:

```python
if my_generation != state.generation_id:
    return  # stale response after interruption
```

When caller speech reaches the barge-in threshold:

```text
caller speech detected
        ↓
increment generation_id
        ↓
stop Twilio playback immediately
        ↓
cancel TTS reader
        ↓
close/end current ElevenLabs turn
        ↓
cancel LLM stream if safe
        ↓
discard queued old audio/text
        ↓
preserve only committed tool side-effects
        ↓
process new user utterance
```

This prevents a particularly unhuman failure mode:

> Caller: “Actually, Thursday—”  
> Agent continues for another 2 seconds: “—and I can also tell you about our whitening services…”

### Distinguish spoken state from committed business state

There are three kinds of state:

```text
Generated text
    ≠
Spoken text
    ≠
Committed external action
```

Suppose the model generates:

> “Perfect, I've booked you for Tuesday at four.”

If the tool has not succeeded, that sentence must never enter TTS.

Suppose the tool **did** succeed but the caller interrupts before hearing the confirmation. The booking still exists.

Therefore track:

```python
tool_effect_committed = True
confirmation_generated = True
confirmation_spoken = False
```

After interruption:

> “Yep — just so you have it, that booking did go through for Tuesday at four.”

That is much more robust than deriving state from conversation text.

### Add a semantic next-action layer

The LLM should ideally decide among a bounded set of conversational acts rather than inventing both business state and prose simultaneously:

```python
class SpeechAct(StrEnum):
    ANSWER = "answer"
    ACKNOWLEDGE = "acknowledge"
    ASK_SLOT = "ask_slot"
    REPAIR = "repair"
    CONFIRM = "confirm"
    TOOL_INTENT = "tool_intent"
    SAFETY_ESCALATE = "safety_escalate"
    CLOSE = "close"
```

An internal plan might be:

```json
{
  "speech_act": "ask_slot",
  "intent": "book_appointment",
  "missing_slot": "preferred_time",
  "tool_action": null,
  "tone": "brief_friendly"
}
```

Then the language model's expressive freedom is constrained to:

> “Gotcha — Tuesday works. Is morning or afternoon easier?”

That is what your earlier work refers to as the next-action/business-logic layer; it is the correct architectural direction. fileciteturn0file0

### Use latency-aware speech only when it is semantically safe

Sometimes the system knows a tool operation will take longer than a comfortable conversational pause.

Instead of a generic:

> “Umm…”

use a truthful, contextual phrase:

> “Gotcha — let me check Tuesday afternoon.”

Then start the tool request immediately.

But this must obey your existing **WAIT-PROMISE ↔ TOOL-CALL law**. The brief explicitly identifies that rule as load-bearing, so it must remain unchanged. fileciteturn0file1

A latency-aware acknowledgement is allowed only when:

```text
we genuinely understood enough to acknowledge
AND
the action mentioned is actually starting now
AND
the acknowledgment cannot falsely imply success
```

### Fast fallbacks should degrade gracefully

Define a deadline by state rather than hanging indefinitely:

```text
0–650 ms:
    preferred LLM/model

provider 429 / connection failure:
    immediate alternate provider

~900 ms without useful text:
    safe contextual acknowledgment,
    but only if business state permits it

tool slow:
    truthful waiting phrase once

all model providers unavailable:
    deterministic service-recovery message

tool unavailable:
    never claim success;
    collect callback details or follow existing escalation
```

Do not say:

> “One moment while I pull that up…”

three times.

One acknowledgement buys you social time. Repeated filler reveals that the system is stalled.

## Instrumentation, benchmarking and validation

### Build one turn-level latency trace

Every turn should emit a single structured trace containing at least:

```json
{
  "call_id": "...",
  "turn_id": 7,
  "generation_id": 12,

  "caller_region": "US",
  "server_region": "us-east",
  "provider": "groq",
  "model": "openai/gpt-oss-20b",
  "service_tier": null,

  "stt_endpoint_ms": 218,

  "llm_queue_ms": 14,
  "llm_connect_ms": 3,
  "llm_ttft_ms": 271,
  "first_sentence_ready_ms": 356,

  "tts_ws_connect_ms": 82,
  "tts_first_byte_ms": 167,

  "twilio_first_audio_ms": 19,
  "eou_to_first_audio_ms": 786,

  "prompt_tokens": 6032,
  "cached_prompt_tokens": 5120,
  "output_tokens": 22,

  "first_chunk_words": 8,
  "response_words": 21,

  "tool_calls": 0,
  "fallback_count": 0,
  "rate_limited": false,

  "barge_in": false,
  "truncated": false,

  "voice_id": "OZ0L6eISlOejga3XjDFt",
  "tts_stability": 0.40,
  "tts_style": 0.0
}
```

Groq exposes cache usage information for its automatic GPT-OSS prompt cache, so cache effectiveness should be measured rather than assumed. citeturn23search5 OpenAI similarly exposes cached-token information for prompt-cache hits. citeturn14view0turn14view1

### Dashboard design

I would build five operational views.

| Dashboard | Primary charts | Why |
|---|---|---|
| **Caller latency** | EOU→audio p50/p90/p95/p99, LLM TTFT, first-sentence-ready, TTS TTFB | Shows what callers actually experience |
| **Provider/model** | TTFT by provider/model/tier, error rate, 429 rate, fallback rate | Prevents “fast benchmark” models from hiding production instability |
| **Conversation quality** | words/turn, questions/turn, repeated phrase rate, repair rate, barge-in rate, farewell-loop rate | Detects robotic conversational regressions automatically |
| **Business correctness** | tool success, invalid tool calls, unsupported answers, booking confirmation errors, escalation correctness | Ensures humanness never beats correctness |
| **Experiment/economics** | A/B arm, naturalness rating, warmth, competence, task success, $/successful call | Lets you optimise the complete product |

The most important latency plot is a **waterfall distribution**, not an average:

```text
Turn p50                         p95
│                               │
├─ endpoint  220 ms             430 ms
├─ LLM       330 ms             710 ms
├─ sentence   55 ms             120 ms
├─ TTS       155 ms             260 ms
└─ playback   25 ms              50 ms
             ──────             ──────
              785 ms           1,570 ms
```

Only after collecting this on the **same turn IDs** should you declare “the LLM is 70% of latency”.

### Synthetic benchmark workload

Do not benchmark with `"Hello"` and then expect the result to predict your 24k-character receptionist prompt.

Create a stable corpus from real call patterns:

| Workload | Suggested share |
|---|---:|
| Greeting/basic inquiry | 10% |
| Hours/address/service FAQ | 10% |
| Simple appointment booking | 15% |
| Appointment with tool lookup | 15% |
| Reschedule/cancel | 10% |
| Ambiguous date/time | 10% |
| Caller correction or STT repair | 10% |
| Frustrated caller | 5% |
| Emergency/safety path | 5% |
| Interruption/barge-in | 5% |
| Small talk/compliment | 2.5% |
| Goodbye/end-call | 2.5% |

Run each model under:

```text
cold connection
warm connection
cold prompt cache
warm prompt cache
concurrency 1
concurrency 5
concurrency 20
tool-free
single-tool
multi-tool
```

A provider that looks extraordinary at concurrency one but rate-limits at realistic concurrency is not your fastest production model.

### Recommended model-speed A/B matrix

| Arm | Provider/model | Transport | Reasoning | Output cap | Purpose |
|---|---|---|---|---:|---|
| S0 | Current OpenAI gpt-4o-mini | SSE | normal | 300 | Historical control |
| S1 | OpenAI gpt-4o-mini | SSE + Fast if enabled | normal | 96 | Measure current optimisations |
| S2 | Groq GPT-OSS 20B | native SSE | low | 96 | Main low-latency candidate |
| S3 | Groq GPT-OSS 120B | native SSE | low | 96 | Quality/speed trade-off |
| S4 | GPT-5.4 nano | SSE + supported fast tier | none/default | 96 | New OpenAI small-model candidate |
| S5 | GPT-5.4 mini | SSE + supported fast tier | none/default | 96 | Stronger OpenAI fallback |
| S6 | Best OpenAI model | Responses WS | same | 96 | Persistent-connection test |

For each, record:

**TTFT, first useful sentence, first audio, tool accuracy, task success, mean words/turn, cost/turn, 429 rate, p95/p99.**

Do not pick the winner purely by p50 TTFT.

### Humanness A/B matrix

Avoid testing every variable simultaneously. You will not know which change caused the result.

**Prompt experiment:**

| Arm | Prompt | Voice/settings |
|---|---|---|
| H0 | Existing | Existing |
| H1 | New persona + talk policy + examples | Existing |

**Voice experiment after prompt winner:**

| Arm | Voice | Settings |
|---|---|---|
| V0 | Sarah | current |
| V1 | Talia | 0.40 / 0.75 / 0 / boost false |
| V2 | Chelsea | same |
| V3 | Maisie | same |
| V4 | Jade | same |

**Settings experiment after voice winner:**

| Arm | Stability | Style | Boost |
|---|---:|---:|---|
| T0 | .50 | 0 | current |
| T1 | .40 | 0 | false |
| T2 | .33 | 0 | false |
| T3 | .40 | .08 | false |
| T4 | .40 | 0 | true |

Then run a final whole-system A/B:

```text
A = current production
B = winning streaming + model + prompt + voice + settings
```

Voice should be randomised at **call level**, not turn level. A caller hearing Sarah on one turn and Chelsea on the next introduces an obvious confound.

### What humans should rate

Use blind recordings where possible. Ask listeners to rate from 1–7:

| Question | Why |
|---|---|
| “The conversation felt natural.” | Primary humanness |
| “The receptionist sounded warm and friendly.” | Addresses current feedback directly |
| “The receptionist sounded competent.” | Catches filler/over-casual regressions |
| “Responses came at a natural pace.” | Perceived latency |
| “The receptionist seemed to understand the caller.” | Grounding/backchannel quality |
| “I would be comfortable calling this receptionist again.” | Product-level outcome |

Also force a simple preference:

> **Which receptionist would you rather speak to again: A or B?**

Do not ask only:

> “Did this sound human?”

A voice could sound human-like while being irritating, slow or unsafe.

A pilot with roughly 30–50 independent raters can reveal variance and effect direction; use the pilot data to calculate the sample size for the final statistically powered comparison rather than choosing an arbitrary sample size in advance.

### Caller behaviour is even more valuable than ratings

Real US and Pakistani phone calls can reveal whether the upgrade actually landed.

Watch for decreases in:

- caller “hello?” prompts during silence;
- repeated questions because the agent apparently did not understand;
- callers talking over the agent because responses are too long;
- agent continuing after caller interruption;
- abrupt abandonment;
- repeated goodbye loops;
- corrections caused by stacked questions;
- unsolicited comments that the system sounds robotic.

Watch for increases in:

- first-attempt task completion;
- successful appointment completion;
- callers naturally continuing without rephrasing;
- appropriate small talk;
- caller responses that assume shared conversational grounding;
- successful interruption/repair;
- preference for the new variant in blind comparisons.

Backchannels and interruption behaviour are particularly important because natural conversation includes signals of attention and controlled overlap rather than rigid alternate monologues. citeturn8search1turn8search2

## Prioritised implementation checklist and rollout

### The implementation order

The fastest route to a noticeably better product is **not** to simultaneously replace the model, prompt, voice and TTS settings and then dial it once.

Use this sequence:

| Order | Work | Repository area | Acceptance gate |
|---|---|---|---|
| **Immediate** | Establish deployed commit/source of truth | deployment + repo | ZIP and deployed runtime agree |
| **Immediate** | Turn-level monotonic latency trace | `twilio_actor.py` / telemetry | Full same-turn waterfall |
| **Immediate** | Confirm native Groq `stream_complete()` | `groq_llm.py` | First Groq token emitted before completion |
| **Immediate** | Shared Groq/fallback HTTP client | provider layer | Warm connection reuse demonstrated |
| **Immediate** | Replace stale Groq Llama defaults | config/router | GPT-OSS IDs only |
| **Immediate** | GPT-OSS low reasoning effort | Groq payload | No hidden reasoning reaches speech |
| **Immediate** | Reduce conversational output cap | `brain.py` / config | Ordinary replies 10–30 words |
| **Next** | New persona/talking/examples | `prompt.py`, `business.json` | Load-bearing prompt snapshot unchanged |
| **Next** | Turn-scoped ElevenLabs WS | `elevenlabs_tts.py` | One handshake per assistant turn |
| **Next** | Complete-sentence `auto_mode` feeding | `streaming.py` | No partial-token TTS chunks |
| **Next** | Talia/Chelsea voice A/B | tenant config | Blind warmth/naturalness winner |
| **Next** | Stability/settings A/B | ElevenLabs config | Quality gain without material p95 increase |
| **Next** | DialogueState/NextActionPolicy | core agent | Explicit slots/actions/commitments |
| **Next** | Generation-ID barge-in | actor/state | Stale audio impossible after cancellation |
| **Then** | Deterministic FAQ/goodbye/repeat fast paths | business layer | No LLM for known safe responses |
| **Then** | Prompt-cache telemetry and stable-prefix experiment | LLM adapters/prompt | Measured cache hit ratio |
| **Then** | OpenAI Responses persistent WS A/B | OpenAI adapter | Demonstrated end-to-end improvement |
| **Then** | Auto-routing by complexity | router | Better cost/latency with no quality regression |

### Specific Claude Code implementation tasks

For Claude Code, I would give it **small acceptance-tested patches rather than “make the voice agent human”**.

**Patch: Groq transport**

```text
Edit apps/api/app/providers/llm/groq_llm.py.

Requirements:
- Implement a native stream_complete() using Groq SSE streaming.
- Match OpenAILLM/RouterLLM's existing stream event contract exactly.
- Stream text deltas immediately.
- Correctly accumulate streamed tool-call arguments.
- Never expose reasoning/tool JSON to the TTS text stream.
- Replace per-request AsyncClient creation with an application-lifetime HTTP/2 AsyncClient.
- Cleanly close the shared client at application shutdown.
- Add tests proving the first text delta is emitted before the HTTP stream completes.
- Add tests for fragmented tool-call arguments.
- Do not change business prompt behaviour.
```

**Patch: Groq model migration**

```text
Replace developer/free-tier defaults:
llama-3.1-8b-instant -> openai/gpt-oss-20b
llama-3.3-70b-versatile -> openai/gpt-oss-120b

Default receptionist reasoning_effort to "low".
Preserve configuration overrides.
Add a startup warning if a deprecated Groq model ID is configured.
```

That migration is required by Groq's 16 August 2026 deprecation for the affected tiers. citeturn23search0

**Patch: response budgets**

```text
Remove hard-coded max_tokens=300 as the universal voice budget.

Add configurable budgets:
VOICE_REPLY_MAX_TOKENS=96
TOOL_DECISION_MAX_TOKENS=192
TOOL_FINAL_REPLY_MAX_TOKENS=96

Apply the larger budget only where structured tool generation needs it.
Do not truncate precision-critical booking, safety, compliance,
phone-number, date or time output.
Add response_word_count telemetry.
```

**Patch: prompt**

```text
Replace only:
- PERSONA
- HOW YOU ACTUALLY TALK
- EXAMPLES
- explicitly approved mood/mirroring text

Do not modify:
- current date/time wording
- identity lock
- tool definitions
- wait-promise ↔ tool-call law
- date handling
- semantic plan
- time handling
- multi-step intent rules
- hallucination guardrails
- booking confirmation
- phone-number handling
- compliance refusals
- business information

Before editing, snapshot/hash those load-bearing sections.
After editing, fail tests if any load-bearing section changed.
```

Those restrictions come directly from your regression history and research brief. fileciteturn0file1

**Patch: ElevenLabs connection lifetime**

```text
Refactor elevenlabs_tts.py so ws_stream_synthesize() does not create
a new WebSocket for each sentence.

Introduce a turn-scoped TTS stream:
start_turn()
send_sentence()
audio_iterator()
finish()
cancel()

Open one WebSocket at assistant-turn start.
Send the ElevenLabs initialisation payload once.
Send each complete LLM sentence over that connection.
Send EOS exactly once after the assistant turn finishes.
Close immediately on barge-in/cancellation.

Keep:
model_id=eleven_flash_v2_5
auto_mode=true

Never send partial LLM tokens into auto_mode.
```

That maps directly onto ElevenLabs' documented WS interface and `auto_mode` guidance. citeturn23search8turn23search9

**Patch: interruption correctness**

```text
Add generation_id to each conversational turn.

When inbound speech qualifies as barge-in:
- increment generation_id;
- clear pending Twilio audio;
- cancel old ElevenLabs turn;
- cancel old LLM stream where possible;
- ignore all text/audio events carrying an older generation_id.

Never roll back a tool action that already committed externally.
Track tool_effect_committed separately from confirmation_spoken.
```

**Patch: observability**

```text
Emit one structured turn record containing:
stt_endpoint_ms
llm_connect_ms
llm_ttft_ms
first_sentence_ready_ms
tts_ws_connect_ms
tts_first_byte_ms
eou_to_first_audio_ms
provider
model
service_tier
prompt_tokens
cached_tokens
output_tokens
response_words
tool_count
fallback_count
429_count
barge_in
truncated
voice_id
voice settings
```

### Tests that should block deployment

Add at least:

```text
test_groq_streams_before_completion
test_groq_streaming_tool_call_reassembly
test_router_does_not_buffer_native_groq_stream
test_llm_clients_are_reused
test_no_deprecated_groq_default_models

test_load_bearing_prompt_sections_unchanged
test_normal_reply_word_budget
test_booking_confirmation_not_truncated
test_emergency_text_not_style_truncated

test_one_elevenlabs_ws_per_turn
test_auto_mode_only_gets_complete_sentences
test_tts_eos_sent_once
test_barge_in_cancels_old_generation
test_stale_audio_discarded_after_barge_in

test_tool_result_never_spoken_before_commit
test_goodbye_only_occurs_once
```

### Rollout timeline

A sensible rollout can be completed in staged slices rather than one dangerous rewrite:

```mermaid
gantt
    title Voice Agent Speed + Humanness Rollout
    dateFormat  YYYY-MM-DD
    axisFormat  %d %b

    section Measurement
    Reconcile deployed commit               :a1, 2026-08-20, 1d
    Turn-level latency waterfall            :a2, 2026-08-20, 2d
    Baseline synthetic benchmark            :a3, after a2, 1d

    section Pure speed
    Groq native SSE + client reuse          :b1, 2026-08-20, 2d
    GPT-OSS migration + reasoning config    :b2, 2026-08-21, 1d
    Output-budget changes                   :b3, 2026-08-21, 1d
    OpenAI Fast benchmark                   :b4, 2026-08-22, 1d

    section Humanness
    Persona + conversation policy           :c1, 2026-08-21, 2d
    Load-bearing regression tests           :c2, 2026-08-21, 2d
    Voice audition                          :c3, 2026-08-22, 2d

    section TTS architecture
    Turn-scoped ElevenLabs WebSocket         :d1, 2026-08-22, 2d
    Sentence/chunk tuning                    :d2, after d1, 1d
    TTS settings A-B                         :d3, after d2, 2d

    section Conversation engine
    DialogueState + NextActionPolicy         :e1, 2026-08-24, 4d
    Barge-in generation ownership            :e2, 2026-08-25, 3d
    Deterministic fast paths                 :e3, 2026-08-27, 2d

    section Validation
    US + PK real-call experiment             :f1, 2026-08-27, 3d
    Blind perception study                   :f2, 2026-08-28, 3d
    Production winner rollout                :f3, 2026-08-31, 2d
```

### Final architecture decision

I would **not** replace Deepgram + ElevenLabs with OpenAI Realtime at this stage.

OpenAI's native speech-to-speech stack is worth keeping as a benchmark because a speech-native system can reduce the number of serial STT→LLM→TTS stages and offers native interruption-oriented voice operation. But OpenAI itself positions chained voice architectures as appropriate where developers need deterministic workflows, transcripts and granular application control. citeturn16view2 Your receptionist has exactly those requirements, and moving architectures would violate the explicit Flash v2.5 constraint while forcing re-validation of your existing booking, hallucination, tool-call, date/time and compliance behaviour.

The opportunity in the existing stack is already large:

```text
CURRENT EXPERIENCE

Caller stops
    ↓
endpointing
    ↓
LLM may spend ~0.8–2.0s before useful output
    ↓
sentence
    ↓
new TTS WebSocket
    ↓
audio
    ↓
robotic, verbose response


TARGET EXPERIENCE

Caller stops
    ↓
fast endpointing
    ↓
warm Groq/OpenAI connection
    ↓
native SSE within a few hundred ms
    ↓
6–14 useful words form a complete sentence
    ↓
already-open turn-scoped ElevenLabs WebSocket
    ↓
first audio around the sub-second range
    ↓
10–30-word context-aware response
    ↓
caller can interrupt instantly
```

And the corresponding conversational change is just as important:

```text
BEFORE

Caller:
"Tell me about your services?"

Agent:
"Of course! What service do you need,
and do you have a specific date,
and time in mind?"


AFTER

Caller:
"Tell me about your services?"

Agent:
"Yeah — we do general, cosmetic, and emergency dental care.
Were you looking for something specific?"


BEFORE

Caller:
"I'm trying to see... Oliver."

Agent:
"Thanks, Oliver! What service are you looking for,
and do you have a date in mind for your appointment?"


AFTER

Caller:
"I'm trying to see... Oliver."

Agent:
"Sorry — I caught 'Oliver', but I missed the bit before that.
Who were you trying to see?"
```

The old system optimises for **completing a receptionist form**. The new system should optimise for **maintaining conversational grounding while advancing business state one step at a time**.

That is the combination most likely to move caller perception from “robotic assistant” to “warm, responsive receptionist”: **true streaming, no unnecessary connection setup, short semantically useful first chunks, an explicit next-action state machine, contextual rather than decorative backchannels, interruptibility, and a voice selected for ordinary conversation rather than polished business narration.** The evidence on response timing, backchannels and fillers strongly supports that behavioural approach over simply adding more vocal disfluencies. citeturn8search0turn8search1turn9search0turn9search2

The immediate success criterion should therefore be stricter than “it sounds nicer”: **p50 end-of-turn→first-audio below roughly 700–900 ms, ordinary turns around 10–30 words, no repeated goodbye or acknowledgement loops, visibly better interruption recovery, unchanged booking/tool correctness, and a statistically clear blind preference for the new prompt/voice combination.**