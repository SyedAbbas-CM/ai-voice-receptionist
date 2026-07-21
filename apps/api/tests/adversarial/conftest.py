"""Pytest wiring for the adversarial harness.

Adds --run-adversarial CLI flag. Without it, all tests in test_adversarial.py
are skipped. This is opt-in because they cost real LLM tokens (Groq free tier,
but rate-limited) and require the receptionist server to be running on 8001.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-adversarial",
        action="store_true",
        default=False,
        help="Run the LLM-as-caller adversarial suite (opt-in, needs server up)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-adversarial"):
        return
    skip_marker = pytest.mark.skip(reason="use --run-adversarial to run these (opt-in — costs LLM tokens)")
    for item in items:
        if "tests/adversarial/" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip_marker)
