"""The box agent's request counting — it decides when an idle endpoint stops costing money."""
import importlib.util
import os
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent.parent / "box" / "rixi_box_agent.py"
spec = importlib.util.spec_from_file_location("rixi_box_agent", AGENT)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def _reset():
    agent._log_pos.update({"offset": 0, "inode": None})


def test_counts_only_new_requests(tmp_path):
    _reset()
    log = tmp_path / "access.log"
    assert agent.requests_since_last_beat(str(log)) == 0      # no log yet
    log.write_text('{"a":1}\n{"a":2}\n')
    assert agent.requests_since_last_beat(str(log)) == 2
    assert agent.requests_since_last_beat(str(log)) == 0      # nothing new
    with open(log, "a") as f:
        f.write('{"a":3}\n')
    assert agent.requests_since_last_beat(str(log)) == 1


def test_a_rotated_or_truncated_log_is_read_from_the_start(tmp_path):
    _reset()
    log = tmp_path / "access.log"
    log.write_text('{"a":1}\n{"a":2}\n{"a":3}\n')
    assert agent.requests_since_last_beat(str(log)) == 3
    os.replace(log, tmp_path / "access.log.1")                # rotation
    log.write_text('{"a":4}\n')
    assert agent.requests_since_last_beat(str(log)) == 1, "a rotation must not hide traffic"
    log.write_text("")                                        # truncation
    assert agent.requests_since_last_beat(str(log)) == 0
    log.write_text('{"a":5}\n{"a":6}\n')
    assert agent.requests_since_last_beat(str(log)) == 2


def test_model_ready_matches_tag_variants(monkeypatch):
    monkeypatch.setattr(agent, "_get_json", lambda url, timeout=5: {"models": [{"name": "qwen3:0.6b"}]})
    assert agent.model_ready("http://x", "qwen3:0.6b")
    assert agent.model_ready("http://x", "qwen3")
    assert not agent.model_ready("http://x", "llama3")
    monkeypatch.setattr(agent, "_get_json", lambda url, timeout=5: (_ for _ in ()).throw(OSError()))
    assert not agent.model_ready("http://x", "qwen3:0.6b")     # server not up yet
