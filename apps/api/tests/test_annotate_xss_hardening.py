"""Security-review regression: XSS via JSON embedded in <script> tags.

apps/api/app/routes/annotate.py serializes transcript turns + tag
vocabulary + existing annotations into JSON that becomes JavaScript
literals inside a <script> block.  A caller utterance containing
'</script>' or line-separator characters (U+2028 / U+2029) would
break out of the script context and enable script injection.

Security review 2026-08-31 flagged this as HIGH.  This test locks in
the escape helper so the vulnerability cannot regress.
"""
from __future__ import annotations


def test_js_json_escapes_script_terminator():
    """A string containing '</script>' must NOT appear literally in
    the JSON output — the '<' should be \\u003c-escaped."""
    from apps.api.app.routes.annotate import _js_json
    out = _js_json({"text": "hello </script><script>alert(1)</script>"})
    assert "</script>" not in out, (
        f"unescaped </script> in JS-JSON output: {out!r}"
    )
    assert "\\u003c" in out


def test_js_json_escapes_angle_brackets():
    from apps.api.app.routes.annotate import _js_json
    out = _js_json({"text": "<b>bold</b>"})
    assert "<" not in out
    assert ">" not in out
    assert "\\u003c" in out and "\\u003e" in out


def test_js_json_escapes_ampersand():
    from apps.api.app.routes.annotate import _js_json
    out = _js_json({"text": "a & b"})
    assert " & " not in out
    assert "\\u0026" in out


def test_js_json_escapes_line_paragraph_separators():
    """U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR) are
    line terminators in JS but valid string chars in JSON — they
    break the JS parser if left raw."""
    from apps.api.app.routes.annotate import _js_json
    out = _js_json({"text": "a b c"})
    assert " " not in out
    assert " " not in out
    assert "\\u2028" in out
    assert "\\u2029" in out


def test_js_json_preserves_json_validity():
    """After escaping, the output must still be parseable as JSON."""
    import json
    from apps.api.app.routes.annotate import _js_json
    payload = {"text": "hello </script>", "n": 42, "list": [1, 2, "<x>"]}
    out = _js_json(payload)
    # Round-trip through json.loads must recover the original dict.
    parsed = json.loads(out)
    assert parsed == payload


def test_js_json_handles_common_types():
    """Lists, nested dicts, ints, bools, None all work."""
    from apps.api.app.routes.annotate import _js_json
    out = _js_json([
        {"a": 1, "b": True, "c": None, "d": [1, 2, 3]},
        "plain string",
    ])
    import json
    assert json.loads(out) == [
        {"a": 1, "b": True, "c": None, "d": [1, 2, 3]},
        "plain string",
    ]
