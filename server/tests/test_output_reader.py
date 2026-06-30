"""Regression tests for the task output reader's JSON handling.

A task may legitimately print a line that is valid JSON but NOT a dict (e.g. a
bare array `[{...}]`). The reader must not crash on it — historically `.get()` on
a non-dict raised AttributeError, which escaped the per-line `except` and killed
the reader loop, breaking the HTTP proxy for that task.
"""
import io
import threading
from collections import deque

import rixi_server


def _run_reader(monkeypatch, lines):
    """Run the output reader over `lines` and return (captured_responses, info)."""
    captured = []
    monkeypatch.setattr(
        rixi_server.http_proxy_manager,
        "set_response_for_task",
        lambda tid, data: captured.append((tid, data)),
    )
    info = {
        "output_lines": deque(),
        "reader_ready": threading.Event(),
        "offline_package": False,
        "task_logger": None,
    }
    reader = rixi_server._make_reader("task1", lambda: info, proxy_label="")
    reader(io.StringIO("".join(lines)), "output")  # must not raise
    return captured, info


def test_non_dict_json_line_does_not_kill_reader(monkeypatch):
    # A bare JSON array (would crash the old reader), then a real http_response
    # frame that MUST still be processed afterwards.
    captured, info = _run_reader(
        monkeypatch,
        [
            '[{"x": 1}]\n',
            '{"type": "http_response", "request_id": "r1", "data": {"status": 200}}\n',
        ],
    )
    assert len(captured) == 1, "frame after the array line was not processed"
    assert captured[0][0] == "task1"
    assert captured[0][1]["request_id"] == "r1"
    # The reader consumed both lines (it did not break out early).
    assert len(info["output_lines"]) == 2


def test_scalar_and_plain_text_lines_are_ignored(monkeypatch):
    captured, info = _run_reader(
        monkeypatch,
        [
            "42\n",                 # bare JSON number
            '"just a string"\n',    # bare JSON string
            "ordinary log line\n",  # not JSON at all
            '{"type": "http_response", "request_id": "r2", "data": {}}\n',
        ],
    )
    assert len(captured) == 1
    assert captured[0][1]["request_id"] == "r2"
    assert len(info["output_lines"]) == 4


def test_http_response_frame_is_routed(monkeypatch):
    captured, _ = _run_reader(
        monkeypatch,
        ['{"type": "http_response", "request_id": "only", "data": {"body": "ok"}}\n'],
    )
    assert len(captured) == 1
    assert captured[0][1]["request_id"] == "only"
