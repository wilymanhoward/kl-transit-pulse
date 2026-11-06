import json
import logging

from app.logging_setup import JsonFormatter


def record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, "stored %d rows", (88,), None)
    rec.__dict__.update(extra)
    return rec


def test_json_line_has_message_and_extra_fields() -> None:
    line = JsonFormatter().format(record(run_id=7, feed_age_s=3))
    entry = json.loads(line)  # one valid JSON object per line
    assert entry["level"] == "INFO" and entry["logger"] == "app.test"
    assert entry["msg"] == "stored 88 rows"
    assert entry["run_id"] == 7 and entry["feed_age_s"] == 3
    assert "args" not in entry and "lineno" not in entry  # standard attributes left out


def test_exceptions_and_unserialisable_values_still_give_valid_json() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = record(when=object())
        rec.exc_info = sys.exc_info()
    entry = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in entry["exc"]
    assert entry["when"].startswith("<object")  # fell back to str(), didn't crash
