"""VPL provider compilers — one per TTS backend.

Each compiler is a pure function VPLUtterance -> CompiledSpeechPlan.
No network I/O.  Providers consume `CompiledSpeechPlan.request_payload`
and dispatch it themselves.

  compile_elevenlabs(u, *, voice_id, model, output_format, phrasing_hints)
  compile_cartesia(u, *, voice_id, model, output_format)

Both return CompiledSpeechPlan whose:
  * `request_payload` matches the provider's actual REST/SSE shape
  * `unsupported_fields` lists VPL fields the provider can't express
  * `approximations` lists fields we translated but only roughly

Picking a compiler: `get_compiler(provider_name)` returns the callable.
Register new providers with `register_compiler(name, fn)`.
"""
from __future__ import annotations

from typing import Callable

from ..schema import CompiledSpeechPlan, VPLUtterance
from .elevenlabs import compile_elevenlabs
from .cartesia import compile_cartesia


Compiler = Callable[..., CompiledSpeechPlan]


_REGISTRY: dict[str, Compiler] = {
    "elevenlabs": compile_elevenlabs,
    "cartesia": compile_cartesia,
}


def get_compiler(provider: str) -> Compiler:
    """Return the compiler for a provider name.  Raises KeyError if
    unknown — callers should catch and log the unsupported provider
    rather than silently defaulting."""
    try:
        return _REGISTRY[provider]
    except KeyError:
        raise KeyError(
            f"no VPL compiler for provider={provider!r}; "
            f"available: {sorted(_REGISTRY)}",
        )


def register_compiler(name: str, fn: Compiler) -> None:
    """Register a compiler for a new provider (Sprint 10+ Qwen, etc)."""
    _REGISTRY[name] = fn


__all__ = [
    "compile_elevenlabs",
    "compile_cartesia",
    "get_compiler",
    "register_compiler",
    "Compiler",
    "CompiledSpeechPlan",
    "VPLUtterance",
]
