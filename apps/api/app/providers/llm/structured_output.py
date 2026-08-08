"""Cross-family structured-output helpers.

Every provider adapter uses these when the caller passes response_schema=.
Three shapes:
  - OpenAI-compat (openai, openrouter, groq, cerebras, mistral, nvidia,
    together, deepseek, sambanova, cloudflare) → response_format kwarg
  - Anthropic → tool-use forced
  - Gemini → generation_config.response_schema

Each provider adapter calls into here to normalize the input + inject
the right field into its native request payload.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Union

from pydantic import BaseModel


log = logging.getLogger(__name__)


# ── schema normalisation ──────────────────────────────────────────

SchemaInput = Union[type[BaseModel], dict, None]


def _to_json_schema(schema: SchemaInput) -> Optional[dict]:
    """Accept pydantic model class OR dict OR None.  Return JSON Schema
    dict (or None)."""
    if schema is None:
        return None
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    raise TypeError(
        f"response_schema must be pydantic BaseModel class or dict, "
        f"got {type(schema).__name__}"
    )


def _schema_name(schema: SchemaInput) -> str:
    """Extract a stable name for the schema — needed by OpenAI's
    json_schema mode (they require a `name` field)."""
    if schema is None:
        return "reply"
    if isinstance(schema, dict):
        return schema.get("title", "reply")
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.__name__
    return "reply"


# ── OpenAI-compat (works for openai, openrouter, groq, cerebras,
#    mistral, together, deepseek, sambanova, cloudflare, nvidia NIM) ──


def openai_response_format(
    schema: SchemaInput,
    strict: bool = True,
) -> Optional[dict]:
    """Return the `response_format` value for an OpenAI-compat request.

    - If schema is a real schema and strict=True → strict json_schema
    - If schema is None → None (no structured output)

    Callers that only support loose json_object should call
    openai_json_object_mode() instead.
    """
    s = _to_json_schema(schema)
    if s is None:
        return None
    if strict:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _schema_name(schema),
                "schema": s,
                "strict": True,
            },
        }
    # Loose fallback — model guarantees valid JSON but not schema shape
    return {"type": "json_object"}


def openai_json_object_mode() -> dict:
    """For providers that only support loose json_object (Groq older
    versions, some Mistral endpoints).  Guarantees valid JSON, not
    schema shape.  Caller MUST still validate with pydantic."""
    return {"type": "json_object"}


# ── Anthropic (Claude) — tool-use forced ──────────────────────────


def anthropic_tool_for_schema(schema: SchemaInput) -> Optional[dict]:
    """Anthropic doesn't have response_format; the idiomatic way to
    force structured output is to define a single tool whose input
    schema IS the desired schema, then force tool_choice to that tool.

    Returns the tool dict to append to the Anthropic tools=[...] list.
    """
    s = _to_json_schema(schema)
    if s is None:
        return None
    return {
        "name": "emit_reply",
        "description": (
            "Emit the structured reply.  This is the ONLY way to reply — "
            "do not answer in plain text."
        ),
        "input_schema": s,
    }


def anthropic_tool_choice_for_schema(schema: SchemaInput) -> Optional[dict]:
    """Companion to anthropic_tool_for_schema — forces the tool."""
    if schema is None:
        return None
    return {"type": "tool", "name": "emit_reply"}


# ── Gemini — generation_config.response_schema ────────────────────


def gemini_generation_config(
    schema: SchemaInput,
    base: Optional[dict] = None,
) -> dict:
    """Merge structured-output fields into a Gemini generation_config.
    If schema is None, returns base unchanged."""
    cfg = dict(base or {})
    s = _to_json_schema(schema)
    if s is None:
        return cfg
    cfg["responseMimeType"] = "application/json"
    cfg["responseSchema"] = s
    return cfg


# ── loose fallback: prompt wrap ───────────────────────────────────


def prompt_wrap_for_schema(
    messages: list[dict],
    schema: SchemaInput,
) -> list[dict]:
    """Last-resort: neither strict-schema nor json_object supported.
    Prepend a system message begging the model to return valid JSON.
    Not reliable — validate with pydantic and retry on parse failure."""
    s = _to_json_schema(schema)
    if s is None:
        return messages
    instruction = (
        "You MUST reply with a single valid JSON object matching this "
        f"JSON Schema exactly:\n{json.dumps(s, indent=2)}\n"
        "Do NOT include any prose before or after the JSON object. "
        "Do NOT wrap it in markdown fences. Emit only the JSON object."
    )
    return [{"role": "system", "content": instruction}] + list(messages)


# ── validation helper ─────────────────────────────────────────────


def parse_and_validate(text: str, schema: SchemaInput) -> Any:
    """Parse LLM output as JSON, then validate against schema.
    Returns the pydantic model instance OR the raw dict OR raises.

    Handles common mistakes: markdown fences, prose prefixes, trailing
    commas.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    candidate = text.strip()
    # Strip markdown fences.
    if "```" in candidate:
        import re
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
    # Trim to first '{' and last '}' — models sometimes prepend prose.
    lb = candidate.find("{")
    rb = candidate.rfind("}")
    if lb == -1 or rb == -1 or rb <= lb:
        raise ValueError(f"no JSON object found in {text[:200]!r}")
    candidate = candidate[lb : rb + 1]
    obj = json.loads(candidate)
    if schema is None:
        return obj
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(obj)
    return obj  # dict-schema case: caller does their own validation
