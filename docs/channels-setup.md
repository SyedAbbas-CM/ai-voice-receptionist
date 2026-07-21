# Messaging channels: WhatsApp + Telegram

The same receptionist brain handles voice-note conversations on WhatsApp and Telegram. Session is keyed per `(channel, user_id)`, so a WhatsApp caller and the same person on Telegram are treated as separate threads — until you wire cross-channel identity later.

## Flow

```
inbound webhook -> parse_webhook -> IncomingMessage
   (voice message)  \
                     \
                      -> STT (Groq/Deepgram/local) -> user_text
                                                          \
                                                           -> brain (tools, state) -> reply
                                                                                        \
                                                                                         -> TTS (Qwen3/11L/OpenAI) -> audio
                                                                                                                        \
                                                                                                                         -> send_voice (channel) -> user
```

Text messages skip STT and TTS. Voice messages do the full round-trip.

## WhatsApp Business Cloud API

### Setup

1. **developers.facebook.com** → create an app → add **WhatsApp** product.
2. Configuration tab → generate a **temporary access token** (24h) or set up a System User for a permanent one.
3. Note the **phone number ID** (not the phone number itself).
4. **Configuration → Webhook → Edit**:
   - Callback URL: `https://<your-tunnel>/channels/whatsapp/webhook`
   - Verify token: any random string you choose. Put the same value in `.env` as `WHATSAPP_VERIFY_TOKEN`.
5. Subscribe the app to the `messages` field.
6. In `.env`:
   ```
   WHATSAPP_ACCESS_TOKEN=EAAJ...
   WHATSAPP_PHONE_NUMBER_ID=1234567890
   WHATSAPP_VERIFY_TOKEN=<same as above>
   ```

### Test

- Add your own phone as a recipient in the WhatsApp dashboard (free tier requires verified recipients).
- Message your test number.
- Record a voice note; the agent transcribes, thinks, and voice-notes back.

### Voice note format

Meta delivers incoming voice as OGG/Opus. Our TTS returns WAV (Qwen3) or MP3 (11L, OpenAI). WhatsApp accepts both when sent as `type: audio` — but the "voice bubble" UX (looks like a proper voice message) requires OGG/Opus. If that matters for the demo, we'd add an ffmpeg convert step before upload — happy to add on request.

### Cost

Meta bills per **conversation window** (24h). Roughly $0.005–0.09 per conversation depending on country and category (utility, marketing, authentication, service). Service/user-initiated conversations are the cheap ones — ideal for a receptionist.

## Telegram

### Setup

1. Talk to **@BotFather** in Telegram → `/newbot` → follow prompts → copy the token.
2. In `.env`:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AAxxxx...
   ```
3. Set the webhook (one-time):
   ```bash
   curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=https://<your-tunnel>/channels/telegram/webhook"
   ```

### Test

- DM your bot.
- Send text or hold-to-record a voice message.
- Bot replies with matching modality.

### Voice format

Telegram voice messages are OGG/Opus. Our reply auto-picks `sendVoice` (for OGG) or `sendAudio` (for WAV/MP3). Telegram will show a voice bubble only for OGG replies.

### Cost

Free. Zero rate limits for reasonable bot traffic.

## Session unification (not yet implemented)

Today: WhatsApp John and Telegram John are two sessions. When you're ready to link:
- Add a `contacts` table keyed by phone.
- Telegram user IDs won't map to phone numbers directly — you'd ask "what's your phone number?" on first contact to link.
- Alternative: use a magic-link one-time verification on both sides.

Tell me when you want this and I'll add it — it's the same 30-minute change either way.

## Exposing your local server

Both webhooks need a public HTTPS URL. Options:

- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8000` — free, no signup.
- **ngrok**: `ngrok http 8000` — free tier fine for demos.
- Production: any host with HTTPS (Fly.io, Railway, your own VPS).

Once you have the public URL, use it in both Meta's webhook config and the Telegram `setWebhook` call.
