"""Sprint 9b: observability plane for the call runtime.

Two mechanisms, both routed through this module so no other file needs
to know whether OTel is even installed:

  * OpenTelemetry spans — per-turn tracing.  One trace = one caller
    turn.  Six span points on the CallActor boundary map the end-to-end
    latency:
        media_in    (first inbound frame after last agent utterance)
        stt_partial (first partial hypothesis from the STT provider)
        llm_first_token (first streamed token from the dialogue model)
        tts_first_byte (first synthesized audio byte returned)
        playback_ack (first Twilio mark ack for this turn's speech)
        turn_done   (playback complete OR next turn started)

  * Prometheus metrics — /metrics scrape endpoint, five histograms
    plus four counters/gauges.  Cheap enough to update on every event.

Both are DISABLED by default and become active when the corresponding
env var is set:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # OTLP HTTP endpoint
    METRICS_ENABLED=true                                # /metrics endpoint

The module handles missing dependencies gracefully — if opentelemetry
isn't installed, span operations become no-ops instead of raising.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

log = logging.getLogger(__name__)


# ── Prometheus metrics (always registered when prometheus_client present) ──

try:
    from prometheus_client import (
        Counter, Gauge, Histogram, CollectorRegistry,
        make_asgi_app, REGISTRY,
    )
    _PROM_OK = True
except ImportError:  # pragma: no cover — obs is optional at install time
    _PROM_OK = False
    Counter = Gauge = Histogram = CollectorRegistry = None  # type: ignore

# Latency histograms in seconds.  Buckets chosen to match the doc's
# latency budget: p50 <700ms, p95 <1000ms end-to-end.  Fine-grained
# under 1s, coarse above so a 30s call still bins meaningfully.
_LATENCY_BUCKETS = (
    0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.55, 0.70,
    0.90, 1.20, 1.60, 2.20, 3.00, 5.00, 10.0,
)

if _PROM_OK:
    TURN_LATENCY = Histogram(
        "voiceops_turn_latency_seconds",
        "End-of-turn (utterance close) to first audible response byte.",
        labelnames=("tenant_id",),
        buckets=_LATENCY_BUCKETS,
    )
    STT_FIRST_PARTIAL = Histogram(
        "voiceops_stt_first_partial_seconds",
        "First inbound speech frame to first STT partial hypothesis.",
        labelnames=("tenant_id", "provider"),
        buckets=_LATENCY_BUCKETS,
    )
    LLM_FIRST_TOKEN = Histogram(
        "voiceops_llm_first_token_seconds",
        "Final transcript to first LLM streamed token.",
        labelnames=("tenant_id", "provider"),
        buckets=_LATENCY_BUCKETS,
    )
    TTS_FIRST_BYTE = Histogram(
        "voiceops_tts_first_byte_seconds",
        "TTS request send to first synthesized audio byte.",
        labelnames=("tenant_id", "provider"),
        buckets=_LATENCY_BUCKETS,
    )
    BARGE_IN_TO_STOP = Histogram(
        "voiceops_barge_in_to_stop_seconds",
        "Confirmed barge-in event to Twilio clear ack.",
        labelnames=("tenant_id",),
        buckets=_LATENCY_BUCKETS,
    )

    BARGE_IN_TOTAL = Counter(
        "voiceops_barge_in_total",
        "Confirmed caller interruptions (INTERRUPT action from classifier).",
        labelnames=("tenant_id",),
    )
    BACKCHANNEL_TOTAL = Counter(
        "voiceops_backchannel_total",
        "Backchannel utterances that did NOT interrupt (right, mm-hm, yeah).",
        labelnames=("tenant_id",),
    )
    PROVIDER_FALLBACK = Counter(
        "voiceops_provider_fallback_total",
        "Router LLM had to fall over to a secondary provider.",
        labelnames=("from_provider", "to_provider"),
    )
    LLM_ROUTER_HITS = Counter(
        "voiceops_llm_router_hits_total",
        "Which provider actually served each LLM call.",
        labelnames=("provider",),
    )
    HEARD_VS_GENERATED_RATIO = Gauge(
        "voiceops_heard_vs_generated_ratio",
        "Ratio of characters heard by the caller to characters synthesized "
        "on the last completed turn.  1.0 = no interruption, <1 = barge-in.",
        labelnames=("tenant_id",),
    )
    ACTIVE_CALLS = Gauge(
        "voiceops_active_calls",
        "Number of CallActors currently registered (per tenant).",
        labelnames=("tenant_id",),
    )
    # Sprint 9e: two-planner architecture.  hit=true means the
    # performance planner returned a valid delivery in time; hit=false
    # means we degraded to default_delivery_for(speech_act).
    TWO_PLANNER_HIT = Counter(
        "voiceops_two_planner_hit_total",
        "Perf planner returned in time with valid delivery.",
        labelnames=("tenant_id", "hit"),
    )
    TWO_PLANNER_LATENCY = Histogram(
        "voiceops_two_planner_latency_seconds",
        "Performance planner wall-clock latency (LLM call + parse + validate).",
        labelnames=("tenant_id", "hit"),
        buckets=_LATENCY_BUCKETS,
    )
    # Sprint 9f: two-stage barge-in.  outcome tracks what stage-1
    # ducking resolved to.  Ratios you care about:
    #   backchannel_unduck / total → false-alarm rate (higher = better sensitivity)
    #   false_trigger / total       → noise-triggered ducks (higher = tune VAD)
    #   confirmed_interrupt / total → real barge-ins
    STAGE1_DUCK = Counter(
        "voiceops_stage1_duck_total",
        "Stage-1 acoustic ducks fired during agent speech, by resolution.",
        labelnames=("tenant_id", "outcome"),
    )
    # Sprint 10 STREAMING WIRING: per-STT-event + per-turn-event
    # counters.  Answers: "how often is streaming firing?  how many
    # backchannels vs interruptions?  is END_OF_TURN latency healthy?"
    STREAM_EVENT = Counter(
        "voiceops_stream_event_total",
        "Streaming STT events by kind (stt_partial, stt_final, "
        "speech_start, speech_end, stream_failed).",
        labelnames=("tenant_id", "kind"),
    )
    TURN_EVENT = Counter(
        "voiceops_turn_event_total",
        "Semantic turn manager events by kind (eager_end_of_turn, "
        "end_of_turn, turn_resumed, backchannel, interruption, "
        "user_requested_pause, false_interruption).",
        labelnames=("tenant_id", "kind"),
    )
    # Task A (2026-08-06): TTS speech-act cache observability.
    # hit_rate = TTS_CACHE_LOOKUPS{result="hit"} / TTS_CACHE_LOOKUPS{result="*"}
    # Every synth call is logged.  Miss also increments TTS_CACHE_STORES.
    TTS_CACHE_LOOKUPS = Counter(
        "voiceops_tts_cache_lookups_total",
        "TTS synth cache lookups. result=hit|miss|error.",
        labelnames=("result",),
    )
    TTS_CACHE_STORES = Counter(
        "voiceops_tts_cache_stores_total",
        "TTS synth cache writes (each represents a new phrase paid to ElevenLabs).",
    )
    TTS_CACHE_HIT_LATENCY = Histogram(
        "voiceops_tts_cache_hit_latency_seconds",
        "Wall-clock latency for cache hits (disk read).",
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
    )
    TTS_CACHE_MISS_LATENCY = Histogram(
        "voiceops_tts_cache_miss_latency_seconds",
        "Wall-clock latency for cache misses (ElevenLabs synth + disk write).",
        buckets=_LATENCY_BUCKETS,
    )
    TTS_CACHE_SIZE_BYTES = Gauge(
        "voiceops_tts_cache_size_bytes",
        "Total disk bytes used by the TTS cache directory.",
    )
    TTS_CACHE_ENTRIES = Gauge(
        "voiceops_tts_cache_entries",
        "Number of entries currently in the TTS cache.",
    )
    # Task B: reactive brain lane telemetry (may or may not be enabled).
    REACTIVE_BRAIN_LANE = Counter(
        "voiceops_reactive_brain_lane_total",
        "Which lane the reactive brain took: silent|backchannel|commit|error.",
        labelnames=("tenant_id", "lane"),
    )
    REACTIVE_BRAIN_LATENCY = Histogram(
        "voiceops_reactive_brain_latency_seconds",
        "Reactive brain LLM call latency, by lane.",
        labelnames=("tenant_id", "lane"),
        buckets=_LATENCY_BUCKETS,
    )
else:  # pragma: no cover
    class _NoopMetric:
        def labels(self, *args, **kwargs):
            return self
        def observe(self, *args, **kwargs):
            pass
        def inc(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            pass
    TURN_LATENCY = STT_FIRST_PARTIAL = LLM_FIRST_TOKEN = TTS_FIRST_BYTE = _NoopMetric()
    BARGE_IN_TO_STOP = _NoopMetric()
    BARGE_IN_TOTAL = BACKCHANNEL_TOTAL = PROVIDER_FALLBACK = _NoopMetric()
    LLM_ROUTER_HITS = HEARD_VS_GENERATED_RATIO = ACTIVE_CALLS = _NoopMetric()
    TWO_PLANNER_HIT = TWO_PLANNER_LATENCY = _NoopMetric()
    STAGE1_DUCK = _NoopMetric()
    STREAM_EVENT = TURN_EVENT = _NoopMetric()
    TTS_CACHE_LOOKUPS = TTS_CACHE_STORES = _NoopMetric()
    TTS_CACHE_HIT_LATENCY = TTS_CACHE_MISS_LATENCY = _NoopMetric()
    TTS_CACHE_SIZE_BYTES = TTS_CACHE_ENTRIES = _NoopMetric()
    REACTIVE_BRAIN_LANE = REACTIVE_BRAIN_LATENCY = _NoopMetric()


# ── OpenTelemetry setup (lazy, gated by env) ────────────────────────

_TRACER = None
_TRACER_INITIALIZED = False


def _init_tracer() -> None:
    """Idempotent setup of the OTel tracer.  No-op if OTel packages
    aren't installed or if OTEL_EXPORTER_OTLP_ENDPOINT is unset."""
    global _TRACER, _TRACER_INITIALIZED
    if _TRACER_INITIALIZED:
        return
    _TRACER_INITIALIZED = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.info("OTel disabled (OTEL_EXPORTER_OTLP_ENDPOINT unset)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as e:
        log.warning("OTel packages missing, tracing disabled: %s", e)
        return

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "voiceops-api"),
        "service.version": os.environ.get("APP_VERSION", "dev"),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")),
    )
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("voiceops.runtime")
    log.info("OTel tracer initialized: endpoint=%s", endpoint)


def get_tracer():
    """Return the tracer (or a no-op if OTel unavailable)."""
    _init_tracer()
    if _TRACER is not None:
        return _TRACER
    # Fallback no-op
    class _NoopSpan:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_attribute(self, *a, **kw): pass
        def add_event(self, *a, **kw): pass
        def record_exception(self, *a, **kw): pass
        def end(self): pass
    class _NoopTracer:
        def start_as_current_span(self, *a, **kw): return _NoopSpan()
        def start_span(self, *a, **kw): return _NoopSpan()
    return _NoopTracer()


# ── Turn-scoped tracing helper ──────────────────────────────────────

@contextmanager
def turn_span(
    *,
    call_id: str,
    tenant_id: str,
    turn_generation: int,
    name: str = "call.turn",
) -> Iterator["TurnSpan"]:
    """Context manager that opens one root span per caller turn.

    Nested spans (`stt`, `llm`, `tts`) attach to it automatically.

    Usage:
        with turn_span(call_id=..., tenant_id=..., turn_generation=...) as span:
            span.mark("stt_final")
            ...
            span.mark("llm_first_token")
    """
    tracer = get_tracer()
    trace_name = f"{name}#{turn_generation}"
    with tracer.start_as_current_span(trace_name) as otel_span:
        try:
            otel_span.set_attribute("call.id", call_id)
            otel_span.set_attribute("call.tenant_id", tenant_id)
            otel_span.set_attribute("call.turn_generation", turn_generation)
        except Exception:
            pass
        wrapper = TurnSpan(
            otel_span=otel_span,
            call_id=call_id,
            tenant_id=tenant_id,
            turn_generation=turn_generation,
        )
        try:
            yield wrapper
        except Exception as e:
            try:
                otel_span.record_exception(e)
            except Exception:
                pass
            raise
        finally:
            wrapper._finalize()


class TurnSpan:
    """Tracks per-turn timing marks.  Emits OTel events + updates
    Prometheus histograms when the turn ends."""

    __slots__ = (
        "_otel_span", "call_id", "tenant_id", "turn_generation",
        "_marks", "_start_ns",
    )

    def __init__(self, *, otel_span, call_id: str, tenant_id: str,
                 turn_generation: int) -> None:
        self._otel_span = otel_span
        self.call_id = call_id
        self.tenant_id = tenant_id
        self.turn_generation = turn_generation
        self._marks: dict[str, int] = {}
        self._start_ns = time.monotonic_ns()

    def mark(self, name: str, **attributes) -> None:
        """Record a timing checkpoint.  Duplicate marks are ignored
        (first-write-wins) — a stream of STT partials only records
        the first."""
        if name in self._marks:
            return
        now = time.monotonic_ns()
        self._marks[name] = now
        try:
            self._otel_span.add_event(name, attributes=attributes)
        except Exception:
            pass

    def elapsed_ms(self, mark_from: str, mark_to: str) -> Optional[float]:
        """Milliseconds between two named marks, or None if either is
        missing.  Useful for structured logging at turn end."""
        a = self._marks.get(mark_from)
        b = self._marks.get(mark_to)
        if a is None or b is None:
            return None
        return (b - a) / 1_000_000.0

    def set_provider(self, kind: str, name: str) -> None:
        """Record the concrete provider name for later Prometheus label
        emission.  `kind` is one of {stt, llm, tts}."""
        self._marks[f"__provider_{kind}"] = name  # type: ignore[assignment]

    def _provider(self, kind: str) -> str:
        return self._marks.get(f"__provider_{kind}", "unknown")  # type: ignore[return-value]

    def _finalize(self) -> None:
        """Called on context exit — emits Prometheus observations."""
        end_of_turn_ms = self.elapsed_ms("media_in", "tts_first_byte")
        if end_of_turn_ms is not None:
            TURN_LATENCY.labels(tenant_id=self.tenant_id).observe(
                end_of_turn_ms / 1000.0,
            )

        stt_ms = self.elapsed_ms("media_in", "stt_first_partial")
        if stt_ms is not None:
            STT_FIRST_PARTIAL.labels(
                tenant_id=self.tenant_id,
                provider=self._provider("stt"),
            ).observe(stt_ms / 1000.0)

        llm_ms = self.elapsed_ms("stt_final", "llm_first_token")
        if llm_ms is not None:
            LLM_FIRST_TOKEN.labels(
                tenant_id=self.tenant_id,
                provider=self._provider("llm"),
            ).observe(llm_ms / 1000.0)

        tts_ms = self.elapsed_ms("tts_request", "tts_first_byte")
        if tts_ms is not None:
            TTS_FIRST_BYTE.labels(
                tenant_id=self.tenant_id,
                provider=self._provider("tts"),
            ).observe(tts_ms / 1000.0)


# ── /metrics ASGI mount helper ──────────────────────────────────────

def mount_metrics(app) -> None:
    """Attach /metrics to a FastAPI app.  Gated by env so we don't
    expose it in tests unless asked.

    Uses a native FastAPI route rather than app.mount because the
    Starlette Mount behaviour differs from a route for exact-path
    matches (a Mount at /metrics only matches /metrics/*, not /metrics
    itself)."""
    if not _PROM_OK:
        log.warning("prometheus_client missing — /metrics not mounted")
        return
    if os.environ.get("METRICS_ENABLED", "true").lower() != "true":
        log.info("/metrics disabled (METRICS_ENABLED=false)")
        return
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi import Response

        async def _metrics_handler():
            return Response(
                content=generate_latest(REGISTRY),
                media_type=CONTENT_TYPE_LATEST,
            )
        # include_in_schema=False keeps it out of OpenAPI docs.
        app.add_api_route("/metrics", _metrics_handler,
                          methods=["GET"], include_in_schema=False)
        log.info("/metrics endpoint mounted")
    except Exception as e:
        log.exception("failed to mount /metrics: %s", e)


# ── Convenience: emit metrics from ledger state at turn end ─────────

def record_heard_vs_generated(
    *,
    tenant_id: str,
    heard_chars: int,
    generated_chars: int,
) -> None:
    """Update the barge-in-severity gauge.  1.0 = agent finished the
    sentence, <1 = caller cut it off partway."""
    if generated_chars <= 0:
        return
    ratio = min(heard_chars / generated_chars, 1.0)
    HEARD_VS_GENERATED_RATIO.labels(tenant_id=tenant_id).set(ratio)


def record_barge_in(tenant_id: str) -> None:
    BARGE_IN_TOTAL.labels(tenant_id=tenant_id).inc()


def record_backchannel(tenant_id: str) -> None:
    BACKCHANNEL_TOTAL.labels(tenant_id=tenant_id).inc()


def record_provider_fallback(from_provider: str, to_provider: str) -> None:
    PROVIDER_FALLBACK.labels(
        from_provider=from_provider, to_provider=to_provider,
    ).inc()


def record_llm_router_hit(provider: str) -> None:
    LLM_ROUTER_HITS.labels(provider=provider).inc()


def set_active_calls(tenant_id: str, n: int) -> None:
    ACTIVE_CALLS.labels(tenant_id=tenant_id).set(n)


def record_stream_event(tenant_id: str, kind: str) -> None:
    """Sprint 10 STREAMING WIRING: STT-stream event counter.  Also
    writes to the durable call event log so /debug/call/{id} shows
    the streaming timeline."""
    STREAM_EVENT.labels(tenant_id=tenant_id, kind=kind).inc()


def record_turn_event(tenant_id: str, kind: str) -> None:
    """Sprint 10 STREAMING WIRING: semantic turn event counter.
    Kind is one of TurnEventKind values."""
    TURN_EVENT.labels(tenant_id=tenant_id, kind=kind).inc()


def record_stage1_duck(tenant_id: str, outcome: str) -> None:
    """Sprint 9f: log a stage-1 duck outcome.  outcome ∈
    {pending, backchannel_unduck, confirmed_interrupt, false_trigger}.

    `pending` bumps on duck itself so we can see ducks vs resolutions
    in the same rollup (e.g. duck fired but call ended before resolve)."""
    STAGE1_DUCK.labels(tenant_id=tenant_id, outcome=outcome).inc()


def record_two_planner_hit(tenant_id: str, hit: bool, latency_ms: int) -> None:
    """Sprint 9e: performance planner outcome.  hit=True means the LLM
    returned a valid delivery in time; hit=False means we fell back to
    default_delivery_for(speech_act).  Latency observed on both paths
    so we can see how quickly failures fail."""
    hit_label = "true" if hit else "false"
    TWO_PLANNER_HIT.labels(tenant_id=tenant_id, hit=hit_label).inc()
    TWO_PLANNER_LATENCY.labels(
        tenant_id=tenant_id, hit=hit_label,
    ).observe(latency_ms / 1000.0)
