from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[4]

# 2026-08-08: pydantic-settings only populates DECLARED fields from .env,
# not os.environ generally.  But several subsystems (router_llm.py, etc.)
# read env vars directly via os.environ.get() — e.g. LLM_ROUTER_ORDER —
# and those were seeing the SHELL env, not our .env, so silently used
# DEFAULT_ORDER.  Load .env into os.environ here so both paths see the
# same values.  override=False → real shell env still wins if set.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(REPO_ROOT / ".env", override=False)
except Exception:
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = "openai"
    stt_provider: str = "groq"
    tts_provider: str = "browser"

    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = "gpt-4o-mini"
    openai_stt_model: Optional[str] = "whisper-1"
    openai_tts_model: Optional[str] = "gpt-4o-mini-tts"
    openai_tts_voice: Optional[str] = "alloy"

    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = "claude-sonnet-4-6"
    # Prompt caching: cache system + tool defs across turns for 90% cost saving
    # + faster TTFT. Disable for A/B tests only.
    anthropic_prompt_caching: bool = True

    groq_api_key: Optional[str] = None
    groq_model: Optional[str] = "llama-3.3-70b-versatile"
    groq_stt_model: Optional[str] = "whisper-large-v3-turbo"

    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = "gemini-2.5-flash"

    cerebras_api_key: Optional[str] = None
    cerebras_model: Optional[str] = "llama-3.3-70b"

    # Mistral La Plateforme — added 2026-08-04.  8 working models
    # (mistral-large-latest, mistral-small-latest, ministral-3b/8b,
    # codestral, pixtral, open-mistral-nemo).  EU-hosted.
    mistral_api_key: Optional[str] = None
    mistral_model: Optional[str] = "mistral-large-latest"

    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = "meta-llama/llama-3.3-70b-instruct"
    openrouter_site_url: Optional[str] = None
    openrouter_app_name: Optional[str] = "voiceops-ai-agent"

    nvidia_api_key: Optional[str] = None
    # Verified working with tool-calling on the free tier as of 2026-07:
    #   meta/llama-3.1-70b-instruct     — 2-4s, tools work reliably (DEFAULT)
    #   meta/llama-3.3-70b-instruct     — may cold-start ~90s on first call
    # DO NOT USE for tool-calling on the free tier:
    #   meta/llama-3.1-8b-instruct      — times out when tools attached
    #   nvidia/llama-3.1-nemotron-nano-8b-v1  — same
    nvidia_model: Optional[str] = "meta/llama-3.1-70b-instruct"
    nvidia_base_url: Optional[str] = "https://integrate.api.nvidia.com/v1"

    ollama_base_url: Optional[str] = "http://localhost:11434"
    ollama_model: Optional[str] = "qwen2.5:7b"

    # SambaNova Cloud — added 2026-08-06.  Free-tier 10 RPM on
    # Meta-Llama-3.1-405B (only free source of a 405B model).
    # Signup: https://cloud.sambanova.ai/apis
    sambanova_api_key: Optional[str] = None
    sambanova_model: Optional[str] = "Meta-Llama-3.3-70B-Instruct"

    # Cloudflare Workers AI — added 2026-08-06.  Free-tier 10k Neurons/day
    # (~1300 replies).  Edge-hosted, orthogonal bucket.
    # Signup: https://dash.cloudflare.com/sign-up
    cloudflare_api_token: Optional[str] = None
    cloudflare_account_id: Optional[str] = None
    cloudflare_model: Optional[str] = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    # Together AI — added 2026-08-06.  Signup gives $1 credit + :free
    # suffix models with zero cost.
    together_api_key: Optional[str] = None
    together_model: Optional[str] = "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"

    # DeepSeek — added 2026-08-06.  Strong reasoning tier.
    deepseek_api_key: Optional[str] = None
    deepseek_model: Optional[str] = "deepseek-chat"

    # Voice-activity detection: "silero" (2MB ONNX, best), "rms" (zero-dep
    # energy fallback), or "auto" (silero if deps present, else rms).
    vad_kind: str = "auto"

    # PII redaction on stored transcripts: "regex" (local, safe default),
    # "presidio" (NER, needs `pip install presidio-analyzer`), or "noop" (off).
    # Default = regex so we never accidentally store raw phones/cards.
    pii_redactor: str = "regex"

    # TCPA consent provider for outbound: "sqlite" (local, default), "http"
    # (client-hosted webhook), "always" (test-only, never prod), or "" to
    # skip the consent check entirely (use only for internal test numbers).
    consent_provider: str = ""
    consent_db_path: str = ""   # for sqlite kind — defaults to data/consent.db
    consent_http_url: str = ""  # for http kind

    # Tracer: "noop" (default, zero overhead), "memory" (in-process buffer),
    # "print" (one JSON line per span to stdout), "otel" (real OTel with OTLP
    # HTTP exporter — needs `pip install opentelemetry-sdk
    # opentelemetry-exporter-otlp-proto-http`). For OTel, set standard env:
    #   OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
    #   OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(pk:sk)>"
    tracer_kind: str = "noop"
    tracer_service_name: str = "voiceops-ai-agent"

    # RAG for voice-agent knowledge lookups.
    # rag_retriever: "sqlite" (default, zero-config), "noop" (off),
    #   "supabase" (Postgres+pgvector, coming later), "langchain" (adapter).
    # rag_embedder: "local" (BGE-small-en, MPS-friendly, default), "openai".
    rag_retriever: str = "sqlite"
    rag_embedder: str = "local"
    rag_db_path: str = "data/rag/kb.db"
    rag_confidence_threshold: float = 0.7  # >= safe to speak; < 0.4 escalate

    deepgram_api_key: Optional[str] = None
    deepgram_model: Optional[str] = "nova-3"
    deepgram_tts_voice: Optional[str] = "aura-asteria-en"

    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: Optional[str] = "eleven_turbo_v2_5"

    cartesia_api_key: Optional[str] = None
    cartesia_voice_id: Optional[str] = None
    cartesia_model: Optional[str] = "sonic-2"

    local_whisper_model: Optional[str] = "base.en"
    local_whisper_compute: Optional[str] = "int8"
    piper_binary: Optional[str] = "piper"
    piper_model_path: Optional[str] = None

    qwen3_tts_model_id: Optional[str] = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
    qwen3_tts_device: Optional[str] = "cuda:0"
    qwen3_tts_dtype: Optional[str] = "bfloat16"
    qwen3_tts_default_speaker: Optional[str] = "Vivian"
    qwen3_tts_default_language: Optional[str] = "English"
    qwen3_tts_instruct: Optional[str] = None
    qwen3_tts_ref_audio: Optional[str] = None
    qwen3_tts_ref_text: Optional[str] = None

    # Kokoro-82M (~10x faster than Qwen3 on M1 Pro). Apache-2.0.
    # Requires: `pip install kokoro` and system `espeak-ng`.
    # Voice codes: af_heart (default), am_adam, bf_emma, etc.
    kokoro_voice: Optional[str] = "af_heart"
    kokoro_device: Optional[str] = "auto"
    kokoro_lang: Optional[str] = "a"          # 'a'=American, 'b'=British, 'j'=Japanese, ...
    kokoro_sample_rate: Optional[int] = 24000

    # Chatterbox Turbo (MLX) — zero-shot voice cloning on M1 GPU.
    # MIT license, RTF 0.51 on M1 Pro, ~2 GB peak RAM. Model on HF:
    # https://huggingface.co/mlx-community/chatterbox-turbo-8bit
    chatterbox_model: Optional[str] = "mlx-community/chatterbox-turbo-8bit"
    chatterbox_ref_audio: Optional[str] = None      # path to 5-15s clip
    chatterbox_ref_text: Optional[str] = None       # exact transcript of ref clip
    chatterbox_temperature: Optional[float] = 0.7
    # Backend: "auto" prefers onnx and falls back to pytorch. "onnx" (fast,
    # ~0.15 RTF on M1 via CoreML — needs `pip install kokoro-onnx onnxruntime`).
    # "pytorch" (~2.5 RTF on M1 via KPipeline).
    kokoro_backend: Optional[str] = "auto"

    vapi_private_key: Optional[str] = None
    vapi_public_url: Optional[str] = None
    vapi_secret: Optional[str] = None
    vapi_assistant_id: Optional[str] = None
    vapi_phone_number_id: Optional[str] = None

    # Outbound transport: "vapi" (real phone calls) or "local" (Qwen3-TTS emulator)
    outbound_transport: str = "vapi"

    ghl_api_token: Optional[str] = None
    ghl_location_id: Optional[str] = None
    ghl_calendar_id: Optional[str] = None
    ghl_api_version: str = "2021-07-28"

    google_service_account_json: Optional[str] = None
    google_calendar_id: Optional[str] = None
    google_sheet_id: Optional[str] = None
    google_sheet_tab: str = "calls"

    crm_sink: str = "none"  # none | ghl | sheets | ghl+sheets

    calendar_backend: str = "fake"  # fake | google

    compat_api_key: Optional[str] = None  # gate for /v1/* (11L-compat) if set

    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_verify_token: Optional[str] = None
    whatsapp_graph_version: str = "v22.0"
    # 2026-08-01 audit WH-003: Meta app secret to verify X-Hub-Signature-256
    whatsapp_app_secret: Optional[str] = None

    telegram_bot_token: Optional[str] = None
    # 2026-08-01 audit WH-004: Telegram-provided secret for webhook auth
    telegram_webhook_secret: Optional[str] = None

    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_public_url: Optional[str] = None

    signalwire_space_url: Optional[str] = None
    signalwire_project_id: Optional[str] = None
    signalwire_token: Optional[str] = None
    signalwire_phone_number: Optional[str] = None

    telnyx_api_key: Optional[str] = None
    telnyx_phone_number: Optional[str] = None
    telnyx_app_id: Optional[str] = None
    telnyx_public_url: Optional[str] = None
    telnyx_public_key: Optional[str] = None

    plivo_auth_id: Optional[str] = None
    plivo_auth_token: Optional[str] = None
    plivo_phone_number: Optional[str] = None
    plivo_app_id: Optional[str] = None
    plivo_public_url: Optional[str] = None

    # Sprint 9a: gate CallActor-backed Twilio path.  Default false so the
    # legacy TwilioStreamSession keeps running until we've soaked the
    # new path.  Flip to true to route inbound calls through the
    # per-call actor + PlaybackLedger kernel.
    twilio_use_actor: bool = False

    # Sprint 9e: enable the two-planner path (semantic + performance
    # LLM + VPL compilation).  Requires twilio_use_actor=true.  When
    # False, TwilioActorSession uses the direct text-to-TTS path.
    two_planner_enabled: bool = False
    # Timeout for the performance planner LLM call in milliseconds.
    performance_planner_timeout_ms: int = 200
    # Model to use for performance planning.  llama-3.1-8b-instant is
    # the fastest option on Groq that reliably emits valid JSON.
    performance_planner_model: str = "llama-3.1-8b-instant"

    # Sprint 9f: two-stage barge-in.  When enabled, VAD-detected speech
    # during agent SPEAKING first "ducks" outbound audio (stops new
    # frames + attenuates in-flight) then waits up to
    # `barge_stage2_deadline_ms` for the classifier to say
    # INTERRUPT/CONTINUE.  On timeout without classification the duck
    # is released and the agent resumes speaking (false-trigger path).
    two_stage_barge_in_enabled: bool = False
    barge_stage2_deadline_ms: int = 400

    # Fix for quiet-phone-voice complaint (2026-08-04): boost outbound
    # µ-law amplitude before send.  0 = pass through (current behavior),
    # positive values scale up.  Range +0..+12 dB.  Above +6 clipping
    # is likely on peaks.  Applied per-frame in _send_mulaw_frames.
    telephony_output_gain_db: float = 0.0

    # Sprint 10 WIRING (2026-08-04): enable the intelligence kernel
    # on the live call path.  When True:
    #   * ReceptionistBrain emits StatePatches into CallState.dialogue
    #   * TemporalResolver normalizes date/time slots before booking
    #   * CommitCoordinator wraps book_appointment (idempotency + evidence)
    # When False, legacy path unchanged.  Default False so we soak.
    dialogue_kernel_enabled: bool = False

    # Sprint 10 STREAMING WIRING (2026-08-04): route inbound Twilio
    # frames through the StreamingSTTBridge + TurnManager instead of
    # the batch STT path.  Enables partial hypotheses, eager end-of-
    # turn, semantic backchannel/interruption/pause detection, and
    # heard-text reconciliation.
    # Requires DEEPGRAM_API_KEY.  Falls back gracefully if the STT
    # provider doesn't support streaming.
    streaming_stt_enabled: bool = False
    turn_manager_enabled: bool = False

    # Sprint 12 Track A: mailbox handlers spawn+return instead of
    # awaiting long-running LLM/TTS/tool work.  Turn events dispatched
    # during agent speech get processed within ~50ms instead of queueing
    # behind the operation they're meant to interrupt.
    # Flip false to restore pre-Sprint-12 inline-await behavior.
    actor_nonblocking_handlers: bool = True

    # S13-A: prosodic end-of-turn model (pipecat-ai/smart-turn-v3).
    # Runs locally on CPU, ~12ms per inference.  When on, the TurnManager
    # consults the model on every STT final: P<0.30 forces fragment buffer,
    # P>0.75 forces commit.  Kills "cut me off mid-sentence" at the root
    # instead of patching with merge windows.
    smart_turn_enabled: bool = True

    # Task A (2026-08-06): TTS synth cache.  Wraps every TTSProvider
    # call with a hash-keyed disk cache.  Miss = original behavior.
    # Hit = ~10ms disk read, skips ElevenLabs entirely.  Cost saver
    # + latency win for common phrases.
    tts_cache_enabled: bool = True
    tts_cache_max_mb: int = 30
    tts_cache_warm_on_boot: bool = True

    # Task B (2026-08-06): reactive+committed brain.  Shadow build,
    # OFF by default.  When on, brain returns structured JSON with
    # should_speak/backchannel/committed_reply and can decide to
    # listen silently across multiple partials.
    reactive_brain_enabled: bool = False

    database_url: Optional[str] = None
    business_profile_path: Optional[str] = None
    calendar_path: Optional[str] = None

    cors_origins: str = "*"

    def model_post_init(self, __context) -> None:
        if not self.database_url:
            self.database_url = f"sqlite:///{REPO_ROOT / 'data' / 'voiceops.db'}"
        if not self.business_profile_path:
            self.business_profile_path = str(REPO_ROOT / "sample-data" / "clinic" / "business.json")
        if not self.calendar_path:
            self.calendar_path = str(REPO_ROOT / "data" / "calendar.json")


settings = Settings()
