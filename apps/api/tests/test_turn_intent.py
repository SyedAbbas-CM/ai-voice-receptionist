"""K3+K4 turn-intent classifier tests."""
from packages.core_agent.classifiers.turn_intent import (
    classify_turn_intent,
    detect_correction_target,
    TurnIntent,
)


def test_correction_no_wait():
    r = classify_turn_intent("no wait actually book it at 4pm not 3pm")
    assert r.intent == TurnIntent.CORRECTION
    assert r.confidence >= 0.8
    assert "CORRECTING" in r.system_note


def test_correction_actually():
    r = classify_turn_intent("actually, I meant Tuesday")
    assert r.intent == TurnIntent.CORRECTION


def test_correction_thats_not():
    r = classify_turn_intent("that's not what I said")
    assert r.intent == TurnIntent.CORRECTION


def test_correction_i_meant():
    r = classify_turn_intent("I meant tooth implants not student plans")
    assert r.intent == TurnIntent.CORRECTION


def test_commitment_yes_book():
    r = classify_turn_intent("yes book it")
    assert r.intent == TurnIntent.COMMITMENT


def test_commitment_sounds_good():
    r = classify_turn_intent("that sounds good, let's do it")
    assert r.intent == TurnIntent.COMMITMENT


def test_rejection_no_thanks():
    r = classify_turn_intent("no thanks, not right now")
    assert r.intent == TurnIntent.REJECTION


def test_clarification_repeat_that():
    r = classify_turn_intent("sorry, can you repeat that?")
    assert r.intent == TurnIntent.CLARIFICATION_REQ


def test_chitchat_how_are_you():
    r = classify_turn_intent("how are you today?")
    assert r.intent == TurnIntent.CHITCHAT


def test_question_default():
    r = classify_turn_intent("what services do you offer?")
    assert r.intent == TurnIntent.QUESTION


def test_answer_default():
    r = classify_turn_intent("my name is John and my phone is 555-1234")
    assert r.intent == TurnIntent.ANSWER


def test_empty_text():
    r = classify_turn_intent("")
    assert r.intent == TurnIntent.UNKNOWN


def test_correction_wins_over_commitment():
    # "no wait, yes book it" -> correction wins because "wait" fires first
    r = classify_turn_intent("no wait, yes book it")
    assert r.intent == TurnIntent.CORRECTION


def test_detect_correction_target_x_not_y():
    assert detect_correction_target("3pm not 4pm") == "3pm"
    assert detect_correction_target("Tuesday not Thursday") == "Tuesday"


def test_detect_correction_target_no_match():
    assert detect_correction_target("book it please") is None
