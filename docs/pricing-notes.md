# Pricing notes (June 2026)

Rough cost-per-call math for picking provider combos. Verify in each dashboard before pitching to a client — vendor prices change.

## Free tiers worth using daily

- **Groq**: Llama 3.3 70B + Whisper-large-v3-turbo, generous free RPD.
- **Deepgram**: $200 free credit. Roughly 50k minutes of nova-3 STT.
- **Gemini**: Free tier on Flash and Flash-Lite. Data used for training on free tier.
- **LiveKit**: 1,000 free agent-session minutes / month.
- **Twilio trial**: 75 voice minutes, 100 SMS, but only verified numbers.
- **Retell AI**: $10 starting credit (pay-as-you-go after).
- **Bland AI**: free Start plan with per-minute usage.

## Rough cost per 3-minute call (cloud stack)

| Item | Provider | Cost |
|------|----------|------|
| LLM (~6k tokens in, 600 out) | OpenAI gpt-4o-mini | ~$0.005 |
| STT (3 min) | Deepgram nova-3 | ~$0.012 |
| TTS (~400 words) | ElevenLabs turbo v2.5 | ~$0.04 |
| Telephony (3 min) | Twilio voice | ~$0.04 |
| **Total** | | **~$0.10 / call** |

Swap ElevenLabs for OpenAI TTS or Cartesia to roughly halve TTS cost. Swap OpenAI LLM for Groq to push LLM cost to near-zero on the free tier.

## What to charge

Common Upwork rates (June 2026 informal scan):
- Vapi/n8n receptionist setup: $300–$1,500 one-time.
- Custom Twilio + OpenAI build: $1,500–$5,000.
- Self-hosted LiveKit/Pipecat: $3,000–$10,000.
- Monthly retainer for monitoring + tweaks: $200–$800.
