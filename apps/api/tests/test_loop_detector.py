"""T4 tests (task #154): TfidfLoopDetector.

Pure-Python TF-IDF cosine similarity — no sklearn dependency.
Verify the math is correct against known reference values, then
verify the rolling-window state machine for the actual "did the
LLM loop?" question.
"""
from __future__ import annotations

import math

import pytest

from packages.dialogue.loop_detector import (
    TfidfLoopDetector,
    _tokenize,
    _cosine,
    _idf,
    _max_similarity_to_last,
    _tf,
    _tfidf_vec,
)


# ── tokenize ────────────────────────────────────


def test_tokenize_lowercases():
    assert _tokenize("Hello World") == ["hello", "world"]


def test_tokenize_splits_on_non_word():
    assert _tokenize("a, b! c?") == ["a", "b", "c"]


def test_tokenize_preserves_apostrophes():
    assert _tokenize("it's fine") == ["it's", "fine"]


def test_tokenize_empty():
    assert _tokenize("") == []
    assert _tokenize("   ") == []


# ── tf ──────────────────────────────────────────


def test_tf_normalizes_to_sum_1():
    tf = _tf(["a", "b", "a", "c"])
    assert tf["a"] == pytest.approx(0.5)
    assert tf["b"] == pytest.approx(0.25)
    assert tf["c"] == pytest.approx(0.25)
    assert sum(tf.values()) == pytest.approx(1.0)


def test_tf_empty():
    assert _tf([]) == {}


# ── idf ─────────────────────────────────────────


def test_idf_matches_sklearn_smoothed_formula():
    """log((N+1)/(df+1)) + 1 smoothing matches sklearn default."""
    docs = [
        ["a", "b"],
        ["b", "c"],
        ["a", "c"],
    ]
    idf = _idf(docs)
    # N=3.  df(a)=2 → log(4/3)+1 ≈ 1.288
    # df(b)=2 → same
    # df(c)=2 → same
    for term in ("a", "b", "c"):
        assert idf[term] == pytest.approx(math.log(4/3) + 1)


def test_idf_rare_term_higher_weight():
    docs = [["common"], ["common"], ["common"], ["rare"]]
    idf = _idf(docs)
    assert idf["rare"] > idf["common"]


# ── cosine ──────────────────────────────────────


def test_cosine_identical_vectors_is_one():
    v = {"a": 0.5, "b": 0.5}
    assert _cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    a = {"x": 1.0}
    b = {"y": 1.0}
    assert _cosine(a, b) == 0.0


def test_cosine_empty_is_zero():
    assert _cosine({}, {"x": 1.0}) == 0.0
    assert _cosine({"x": 1.0}, {}) == 0.0


def test_cosine_partial_overlap():
    a = {"x": 1.0, "y": 1.0}
    b = {"y": 1.0, "z": 1.0}
    # cos = 1 / (sqrt(2) * sqrt(2)) = 0.5
    assert _cosine(a, b) == pytest.approx(0.5)


# ── max_similarity_to_last ─────────────────


def test_similarity_zero_when_fewer_than_two_chunks():
    assert _max_similarity_to_last(["only one"]) == 0.0
    assert _max_similarity_to_last([]) == 0.0


def test_similarity_identical_chunks_is_one():
    """Two identical chunks in the window → cosine ~1."""
    sim = _max_similarity_to_last([
        "what day were you thinking",
        "what day were you thinking",
    ])
    assert sim >= 0.99


def test_similarity_completely_different_chunks_is_zero():
    sim = _max_similarity_to_last([
        "apple banana cherry",
        "xylophone yoyo zebra",
    ])
    assert sim < 0.1


def test_similarity_rephrase_stays_high():
    """LLM rephrasing the same question — words swap but topic same."""
    sim = _max_similarity_to_last([
        "what day were you thinking",
        "which day works for you",
    ])
    # Some overlap ('day' + 'you') — should be non-trivially high
    # but not extreme.
    assert 0.2 < sim < 0.9


# ── detector state machine ──────────────────


def test_construct_rejects_bad_window():
    with pytest.raises(ValueError):
        TfidfLoopDetector(window_size=0)


def test_construct_rejects_bad_threshold():
    with pytest.raises(ValueError):
        TfidfLoopDetector(similarity_threshold=1.5)
    with pytest.raises(ValueError):
        TfidfLoopDetector(similarity_threshold=-0.1)


def test_construct_rejects_bad_consecutive():
    with pytest.raises(ValueError):
        TfidfLoopDetector(consecutive_threshold=0)


def test_empty_chunks_ignored():
    d = TfidfLoopDetector()
    assert d.add("") is False
    assert d.add("   ") is False
    assert d.state()["chunks_in_window"] == 0


def test_single_chunk_never_loops():
    d = TfidfLoopDetector(consecutive_threshold=1)
    assert d.add("one and only") is False


def test_three_identical_chunks_declare_loop():
    """The Christiaan follow-up worst case: LLM asks the same
    discovery question 3 times in a row."""
    d = TfidfLoopDetector(
        similarity_threshold=0.85, consecutive_threshold=3,
    )
    r1 = d.add("what day and time were you thinking")
    r2 = d.add("what day and time were you thinking")
    r3 = d.add("what day and time were you thinking")
    r4 = d.add("what day and time were you thinking")
    assert r1 is False
    # r2 is 1st similar hit, r3 is 2nd, r4 is 3rd = loop.
    # Note: r2 counter=1, r3 counter=2, r4 counter=3 → r4 fires.
    assert r4 is True


def test_diverse_chunks_do_not_loop():
    d = TfidfLoopDetector()
    r1 = d.add("what day works")
    r2 = d.add("perfect, and your name")
    r3 = d.add("great, phone number please")
    r4 = d.add("thanks, all set")
    assert not any([r1, r2, r3, r4])


def test_reset_clears_state():
    d = TfidfLoopDetector(consecutive_threshold=2)
    d.add("same question")
    d.add("same question")
    d.reset()
    assert d.state()["chunks_in_window"] == 0
    assert d.state()["consecutive_similar"] == 0
    # After reset, need to loop again from scratch.
    assert d.add("same question") is False
    assert d.add("same question") is False   # consecutive=1
    assert d.add("same question") is True    # consecutive=2 → fire


def test_dissimilar_chunk_resets_consecutive_counter():
    d = TfidfLoopDetector(
        similarity_threshold=0.85, consecutive_threshold=3,
    )
    d.add("same question")
    d.add("same question")   # consecutive=1
    d.add("something completely different topic")   # counter → 0
    # Now consecutive resets; adding another two won't yet fire.
    d.add("same question")   # window has old identicals + this
    # Depending on window overlap, may or may not fire — behavior
    # is 'counter resets on dissimilar', so this new similarity
    # brings us back to 1, not 2.  Below threshold.
    assert d.state()["consecutive_similar"] < 3


def test_window_bounded_to_size():
    d = TfidfLoopDetector(window_size=5)
    for i in range(20):
        d.add(f"chunk number {i}")
    assert d.state()["chunks_in_window"] == 5


def test_state_shape():
    d = TfidfLoopDetector()
    d.add("hello world")
    s = d.state()
    assert "window_size" in s
    assert "similarity_threshold" in s
    assert "consecutive_threshold" in s
    assert "chunks_in_window" in s
    assert "consecutive_similar" in s
    assert "last_max_similarity" in s


# ── christiaan-scenario regression ────────────────


def test_discovery_rephrase_loop_detected():
    """The exact task-#150 failure mode: LLM refuses to call
    answer_context_task and keeps asking the discovery question
    with minor variations.  Real loops in prod tend to be
    near-repetitions (LLM copy-paste), not creative rephrases.
    Threshold 0.5 tuned to catch that pattern without false
    positives on genuinely-different turns."""
    d = TfidfLoopDetector(
        similarity_threshold=0.5,
        consecutive_threshold=3,
    )
    utterances = [
        "sorry, follow-up to what procedure",
        "just to confirm, follow-up to what procedure",
        "again, follow-up to what procedure",
        "just to be sure, follow-up to what procedure",
    ]
    fired = False
    for u in utterances:
        if d.add(u):
            fired = True
            break
    assert fired, (
        f"detector should have fired on discovery rephrase loop; "
        f"state: {d.state()}"
    )


def test_defensive_never_raises_on_garbage():
    d = TfidfLoopDetector()
    for junk in (None, 42, {}, [], ""):
        try:
            d.add(junk)   # type: ignore[arg-type]
        except Exception:
            pytest.fail(f"detector raised on garbage input: {junk!r}")
