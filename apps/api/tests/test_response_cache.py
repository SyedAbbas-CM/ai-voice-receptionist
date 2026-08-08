"""Tests for the response cache — normalization + hit/miss + slot rejection."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packages.response_cache import ResponseCache, normalize_input


@pytest.fixture
def cache() -> ResponseCache:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    c = ResponseCache(tmp.name)
    yield c
    Path(tmp.name).unlink(missing_ok=True)


# ── normalization ─────────────────────────────────────────────────────

def test_normalize_strips_fillers():
    assert normalize_input("Um, hi are you open saturdays?") == "are you open saturdays"
    assert normalize_input("Uh, yeah so do you take Aetna?") == "do you take aetna"
    assert normalize_input("Hey how much is a cleaning?") == "how much is a cleaning"


def test_normalize_collapses_similar_wording():
    # Same wording with different fillers + punctuation → same key
    a = normalize_input("Hi are you open Saturdays?")
    b = normalize_input("hey, are you open saturdays")
    c = normalize_input("uh are you open saturdays please")
    assert a == b == c
    # Materially different wording ("are you" vs "you") is intentionally
    # NOT collapsed — false cache hits would send wrong replies.


def test_normalize_rejects_time_slot():
    # Cache-buster: unique time = don't cache
    assert normalize_input("book me at 3:30 tomorrow") == ""
    assert normalize_input("can I get 10:00 monday") == ""


def test_normalize_rejects_dates():
    assert normalize_input("appointment on tuesday") == ""
    assert normalize_input("do you have something tomorrow") == ""


def test_normalize_rejects_pii():
    assert normalize_input("my number is 415-555-0134") == ""
    assert normalize_input("email me at foo@bar.com") == ""
    assert normalize_input("hi my name is Sarah Wilson") == ""


def test_normalize_allows_common_generic_questions():
    assert normalize_input("what services do you offer") != ""
    assert normalize_input("are you accepting new patients") != ""
    assert normalize_input("do you take blue cross") != ""


def test_normalize_rejects_too_short():
    assert normalize_input("ok") == ""
    assert normalize_input("yes") == ""


# ── cache get/put ──────────────────────────────────────────────────────

def test_cache_miss_returns_none(cache):
    assert cache.get("biz1", "tenant1", "are you open saturdays") is None


def test_cache_hit_after_put(cache):
    cache.put("biz1", "tenant1", "Are you open Saturdays?",
              "Yep, Saturday 8 to 1.")
    hit = cache.get("biz1", "tenant1", "hi, are you open saturdays?")
    assert hit is not None
    assert hit.reply_text == "Yep, Saturday 8 to 1."
    assert hit.hits == 1


def test_cache_hits_increment(cache):
    cache.put("biz1", "t1", "hours?", "Eight to five weekdays")
    cache.get("biz1", "t1", "hours?")
    cache.get("biz1", "t1", "hours?")
    entry = cache.get("biz1", "t1", "hours?")
    assert entry.hits == 3


def test_cache_per_business_isolation(cache):
    cache.put("dental", "t1", "hours?", "Eight to five dental")
    cache.put("clinic", "t1", "hours?", "Nine to six clinic")
    assert cache.get("dental", "t1", "hours?").reply_text == "Eight to five dental"
    assert cache.get("clinic", "t1", "hours?").reply_text == "Nine to six clinic"


def test_cache_per_tenant_isolation(cache):
    cache.put("dental", "tenantA", "hours?", "Tenant A hours here")
    cache.put("dental", "tenantB", "hours?", "Tenant B hours here")
    assert cache.get("dental", "tenantA", "hours?").reply_text == "Tenant A hours here"
    assert cache.get("dental", "tenantB", "hours?").reply_text == "Tenant B hours here"


def test_cache_ignores_uncacheable(cache):
    # Time-bearing input should be a no-op put + always miss
    key = cache.put("biz", "t1", "book me at 3:30 tomorrow", "Booked!")
    assert key is None
    assert cache.get("biz", "t1", "book me at 3:30 tomorrow") is None


def test_cache_invalidate_business(cache):
    cache.put("biz1", "t1", "hours?", "8-5")
    cache.put("biz1", "t1", "insurance?", "yes we take most")
    cache.put("biz2", "t1", "hours?", "10-6")
    dropped = cache.invalidate_business("biz1", "t1")
    assert dropped == 2
    assert cache.get("biz1", "t1", "hours?") is None
    assert cache.get("biz2", "t1", "hours?") is not None


def test_cache_top_hits(cache):
    cache.put("biz", "t1", "hours?", "8 to 5 Mon-Fri")
    cache.put("biz", "t1", "insurance?", "Yes we take most major insurance")
    for _ in range(5):
        cache.get("biz", "t1", "insurance?")
    for _ in range(2):
        cache.get("biz", "t1", "hours?")
    top = cache.top_hits("biz", "t1", limit=10)
    assert len(top) == 2
    assert top[0].reply_text == "Yes we take most major insurance"
    assert top[0].hits == 5


def test_cache_replaces_reply_when_answer_improves(cache):
    cache.put("biz", "t1", "hours?", "old bad answer")
    cache.get("biz", "t1", "hours?")   # hits = 1
    cache.put("biz", "t1", "hours?", "new better answer")
    entry = cache.get("biz", "t1", "hours?")
    assert entry.reply_text == "new better answer"
    # Hits preserved from before update
    assert entry.hits >= 2
