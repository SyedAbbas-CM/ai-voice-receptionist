"""Provider-swappable tracer for LLM/STT/TTS/tool spans.

Every real production voice agent needs:
  - Per-stage latency (P50/P95/P99 for STT, LLM, tool, TTS)
  - Per-call cost decomposition
  - Traceable failures ("why did this call take 8s?")

We don't want to force a heavy OTel install on the demo path — so this
module gives four backends behind ONE interface:

  - NoopTracer      — off. Zero overhead.
  - InMemoryTracer  — collects spans in a list. Great for tests + debugging.
  - PrintTracer     — writes each span to stdout as a JSON line. Local dev.
  - OTelTracer      — real OpenTelemetry with configurable OTLP exporter
                      (Langfuse, Honeycomb, Grafana Tempo, Jaeger, etc).

Span names follow OpenTelemetry GenAI semantic conventions:
  gen_ai.system, gen_ai.request.model, gen_ai.usage.input_tokens, ...
  https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


log = logging.getLogger(__name__)


@dataclass
class Span:
    """One recorded operation. Kept simple so backends can translate."""
    name: str
    start_ms: float
    end_ms: Optional[float] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok | error
    error_message: Optional[str] = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[float]:
        return (self.end_ms - self.start_ms) if self.end_ms is not None else None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: str, message: Optional[str] = None) -> None:
        self.status = status
        if message:
            self.error_message = message


class Tracer(ABC):
    name: str = "base"

    @abstractmethod
    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[Span]:
        """Yield a Span; automatically close on __exit__."""
        yield  # pragma: no cover

    def record_llm(
        self, provider: str, model: str, input_tokens: int = 0,
        output_tokens: int = 0, cache_read_tokens: int = 0, latency_ms: Optional[float] = None,
    ) -> None:
        """Convenience helper — record an LLM call span in one line."""
        with self.span(
            "gen_ai.chat_completion",
            **{
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
                "gen_ai.usage.cache_read_input_tokens": cache_read_tokens,
            },
        ) as span:
            if latency_ms is not None:
                span.set_attribute("latency_ms", latency_ms)


class NoopTracer(Tracer):
    """Zero overhead. Default when observability is off."""
    name = "noop"

    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[Span]:
        s = Span(name=name, start_ms=0.0, end_ms=0.0, attributes=attributes)
        yield s


class InMemoryTracer(Tracer):
    """Collects every span into a list. Perfect for unit tests + a
    /debug/traces endpoint in a dashboard."""
    name = "memory"

    def __init__(self, max_spans: int = 10000):
        self.spans: list[Span] = []
        self.max_spans = max_spans

    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[Span]:
        s = Span(name=name, start_ms=time.time() * 1000, attributes=dict(attributes))
        try:
            yield s
        except Exception as e:
            s.set_status("error", str(e))
            raise
        finally:
            s.end_ms = time.time() * 1000
            if len(self.spans) < self.max_spans:
                self.spans.append(s)


class PrintTracer(Tracer):
    """Writes each finished span to stdout as one JSON line. Grep-friendly."""
    name = "print"

    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[Span]:
        s = Span(name=name, start_ms=time.time() * 1000, attributes=dict(attributes))
        try:
            yield s
        except Exception as e:
            s.set_status("error", str(e))
            raise
        finally:
            s.end_ms = time.time() * 1000
            print(json.dumps({
                "span": s.name,
                "duration_ms": s.duration_ms,
                "status": s.status,
                "attributes": s.attributes,
                "error": s.error_message,
            }, default=str), file=sys.stdout, flush=True)


class OTelTracer(Tracer):
    """Real OpenTelemetry via opentelemetry-sdk. Lazy-imports so we don't
    force the dep on the local demo path.

    Configure the OTLP endpoint via env:
        OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com
        OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic ...

    All standard OTel env vars work — this is intentional so any OTel-compatible
    sink (Langfuse, Honeycomb, Jaeger, Grafana Tempo) works with zero code."""
    name = "otel"

    def __init__(self, service_name: str = "voiceops-ai-agent"):
        self.service_name = service_name
        self._otel_tracer = None

    def _load(self):
        if self._otel_tracer is not None:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError as e:
            raise RuntimeError(
                "OTelTracer needs `pip install opentelemetry-sdk "
                "opentelemetry-exporter-otlp-proto-http`. Falling back to Noop "
                "or use kind='print' for local logging."
            ) from e

        resource = Resource.create({"service.name": self.service_name})
        provider = TracerProvider(resource=resource)
        # OTLP HTTP exporter reads OTEL_EXPORTER_OTLP_ENDPOINT + _HEADERS from env
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        self._otel_tracer = trace.get_tracer(self.service_name)

    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[Span]:
        try:
            self._load()
        except RuntimeError as e:
            log.warning("OTel unavailable, span dropped: %s", e)
            s = Span(name=name, start_ms=time.time() * 1000, attributes=dict(attributes))
            s.end_ms = s.start_ms
            yield s
            return

        with self._otel_tracer.start_as_current_span(name) as otel_span:
            s = Span(name=name, start_ms=time.time() * 1000, attributes=dict(attributes))
            for k, v in attributes.items():
                otel_span.set_attribute(k, v)
            try:
                yield s
            except Exception as e:
                s.set_status("error", str(e))
                otel_span.set_status(otel_span.status.__class__(status_code=2))
                otel_span.record_exception(e)
                raise
            finally:
                s.end_ms = time.time() * 1000
                for k, v in s.attributes.items():
                    otel_span.set_attribute(k, v)


def build_tracer(kind: str = "noop", **kwargs) -> Tracer:
    """Factory. Env-swappable via settings.tracer_kind."""
    kind = (kind or "noop").lower()
    if kind == "noop":
        return NoopTracer()
    if kind == "memory":
        return InMemoryTracer(**kwargs)
    if kind == "print":
        return PrintTracer()
    if kind == "otel":
        return OTelTracer(**kwargs)
    raise ValueError(f"unknown tracer kind: {kind!r}")


# ---- Module-level singleton the app uses everywhere ----

_tracer_singleton: Tracer = NoopTracer()


def get_tracer() -> Tracer:
    return _tracer_singleton


def set_tracer(tracer: Tracer) -> None:
    global _tracer_singleton
    _tracer_singleton = tracer


# ── LangChain-project port: async decorator + tri-sink init (2026-08-10) ──
#
# Ported from /Users/az/Desktop/LangChain/src/observability/tracer.py.
# That project runs Phoenix (local UI) + LangSmith (cloud) + Langfuse
# (self-hosted) in parallel — each sink env-gated so you can turn any of
# them on/off without touching code.  For voice-AI where every turn is a
# ~2s critical path this gives us: latency per span, per-call cost, per-
# provider p95, error taxonomy — all viewable in Phoenix's local UI or
# shipped to LangSmith/Langfuse for team dashboards.

import functools as _functools
import os as _os
from contextlib import asynccontextmanager
from typing import Callable as _Callable


def init_tri_sink_tracing(service_name: str = "voiceops-ai-agent") -> None:
    """Idempotent per-sink init driven by env vars.  Any combination:

      LANGSMITH_TRACING=true + LANGSMITH_API_KEY=ls_...
      PHOENIX_ENABLED=true + PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
      LANGFUSE_ENABLED=true + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY

    Also sets our module singleton to OTelTracer so `get_tracer()` picks
    up the Phoenix/LangSmith exporter chain automatically.

    Safe to call multiple times."""
    if _os.getenv("PHOENIX_ENABLED", "").lower() in ("true", "1", "yes"):
        try:
            from phoenix.otel import register as _phx_register
            _phx_register(project_name=service_name, auto_instrument=True)
            log.info("phoenix tracing enabled (%s)",
                     _os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "local"))
        except ImportError:
            log.warning("PHOENIX_ENABLED set but arize-phoenix not installed")

    if _os.getenv("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes"):
        try:
            import langsmith  # noqa: F401 — env-var auto-config
            log.info("langsmith tracing enabled (project=%s)",
                     _os.getenv("LANGSMITH_PROJECT", service_name))
        except ImportError:
            log.warning("LANGSMITH_TRACING set but langsmith not installed")

    if _os.getenv("LANGFUSE_ENABLED", "").lower() in ("true", "1", "yes"):
        try:
            from langfuse import Langfuse  # noqa: F401
            log.info("langfuse tracing enabled")
        except ImportError:
            log.warning("LANGFUSE_ENABLED set but langfuse not installed")

    # If ANY sink is enabled, install OTelTracer.  Otherwise leave the
    # singleton as-is (usually NoopTracer or whatever main.py wired).
    any_sink = any(
        _os.getenv(k, "").lower() in ("true", "1", "yes")
        for k in ("PHOENIX_ENABLED", "LANGSMITH_TRACING", "LANGFUSE_ENABLED")
    )
    if any_sink and not isinstance(_tracer_singleton, OTelTracer):
        set_tracer(OTelTracer(service_name=service_name))


@asynccontextmanager
async def trace_context(name: str, **attrs):
    """Async context manager wrapping the current tracer's `span()`.
    Yields the Span so callers can attach attrs mid-op:

        async with trace_context("rag.retrieve", tenant_id="x") as s:
            docs = await retriever(query)
            s.set_attribute("hits", len(docs))
    """
    with get_tracer().span(name, **attrs) as s:
        yield s


def atraced(name: Optional[str] = None):
    """Async function decorator.  Wraps the call in trace_context.
    Ported from LangChain project — makes it trivial to instrument any
    async RAG/LLM/TTS call with one line:

        @atraced("rag.retrieve")
        async def retrieve(...): ...
    """
    def deco(fn: _Callable):
        label = name or fn.__qualname__
        @_functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with trace_context(label):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


def span_output(span: Any, output: dict) -> None:
    """Attach output fields as `output.{k}` span attributes.  Matches
    OpenInference GenAI conventions Phoenix expects for auto-render."""
    if span is None:
        return
    for k, v in output.items():
        try:
            span.set_attribute(f"output.{k}", v)
        except Exception:
            pass


def flush() -> None:
    """Force-flush all sinks before process exit.  Call in shutdown hook.
    Safe if nothing is registered."""
    try:
        from opentelemetry import trace as _otel_trace
        provider = _otel_trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception:
        pass
