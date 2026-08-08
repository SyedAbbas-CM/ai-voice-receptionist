"""Task #243: llm_discover unit tests.  Mocks httpx so no live API calls."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from packages.runtime.llm_discover import (
    discover_all,
    validate_alternates,
    LiveModel,
    DiscoveryResult,
)


def test_validate_alternates_flags_dead_models():
    result = DiscoveryResult(
        when=0.0, duration_s=0.0, errors={},
        by_provider={
            "groq": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        },
        models=[],
    )
    configured = {
        "groq": ["openai/gpt-oss-120b", "kimi-k2-instruct"],  # 2nd is dead
    }
    out = validate_alternates(result, configured)
    assert out["groq"]["openai/gpt-oss-120b"] is True
    assert out["groq"]["kimi-k2-instruct"] is False


def test_validate_alternates_unknown_provider():
    result = DiscoveryResult(
        when=0.0, duration_s=0.0, errors={}, by_provider={}, models=[],
    )
    configured = {"some-provider-with-no-live-scan": ["x", "y"]}
    out = validate_alternates(result, configured)
    # Provider not scanned → all models flagged as not-live
    assert out["some-provider-with-no-live-scan"] == {"x": False, "y": False}


def test_discover_all_skips_providers_without_keys():
    """discover_all should silently skip any provider whose env key is unset."""
    async def _run():
        # empty env → all providers skipped, zero tasks spawned
        result = await discover_all(env={})
        assert result.by_provider == {}
        assert result.errors == {}
        assert len(result.models) == 0
    asyncio.run(_run())
