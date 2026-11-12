from __future__ import annotations

import pytest

from agentlens import config, errlog, paths
from agentlens.events import (
    Category,
    Event,
    Provenance,
    TsSource,
    payload_event_id,
    tool_event_id,
)


def make(**over):
    base = dict(
        event_id="a" * 64,
        provider="claude-code",
        session_id="s1",
        project="/p",
        category=Category.TOOL,
        type="tool.post",
        ts=1700000000.5,
        ts_source=TsSource.COLLECTOR,
        source="hook",
        provenance=Provenance.OBSERVED,
        tool_name="Bash",
        tool_use_id="t1",
        status="ok",
        duration_ms=12,
        data={"x": 1},
    )
    base.update(over)
    return Event(**base)


def test_provenance_values():
    assert {p.value for p in Provenance} == {"observed", "calculated", "estimated"}


def test_category_values_match_spec():
    assert {c.value for c in Category} == {
        "session", "prompt", "tool", "file", "usage",
        "performance", "error", "task", "approval",
    }


def test_round_trip():
    ev = make()
    d = ev.to_dict()
    assert d["category"] == "tool" and d["provenance"] == "observed" and d["ts_source"] == "collector"
    assert Event.from_dict(d) == ev


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("session_id"),
        lambda d: d.update(category="nope"),
        lambda d: d.update(provenance="guessed"),
        lambda d: d.update(event_id="short"),
        lambda d: d.update(data=[1, 2]),
        lambda d: d.update(duration_ms="12"),
        lambda d: d.update(session_id=""),
    ],
)
def test_from_dict_rejects_invalid(mutate):
    d = make().to_dict()
    mutate(d)
    with pytest.raises(ValueError):
        Event.from_dict(d)


def test_from_dict_rejects_non_object():
    with pytest.raises(ValueError):
        Event.from_dict(["not", "an", "event"])


def test_tool_event_id_stable_and_distinct():
    a = tool_event_id("claude-code", "s", "PreToolUse", "t1")
    assert a == tool_event_id("claude-code", "s", "PreToolUse", "t1")
    assert a != tool_event_id("claude-code", "s", "PostToolUse", "t1")
    assert a != tool_event_id("claude-code", "s", "PreToolUse", "t2")
    assert len(a) == 64


def test_tool_event_id_no_concatenation_collisions():
    assert tool_event_id("p", "ab", "c", "d") != tool_event_id("p", "a", "bc", "d")


def test_payload_event_id_truncates_to_second():
    p = {"session_id": "s", "hook_event_name": "Stop"}
    assert payload_event_id("cc", p, 100.1) == payload_event_id("cc", p, 100.9)
    assert payload_event_id("cc", p, 100.9) != payload_event_id("cc", p, 101.0)
    # key order does not matter
    assert payload_event_id("cc", {"b": 1, "a": 2}, 5) == payload_event_id("cc", {"a": 2, "b": 1}, 5)


def test_config_defaults_and_override():
    assert config.load()["capture_prompts"] is False
    paths.ensure_dir(paths.home())
    paths.config_path().write_text('{"capture_prompts": true}')
    assert config.load()["capture_prompts"] is True
    paths.config_path().write_text('{"capture_prompts": "yes"}')  # wrong type ignored
    assert config.load()["capture_prompts"] is False
    paths.config_path().write_text("{broken")
    assert config.load()["capture_prompts"] is False


def test_errlog_writes_redacted_and_never_raises():
    try:
        raise RuntimeError("failed with API_KEY=supersecretvalue")
    except RuntimeError as exc:
        errlog.log_error("ctx", exc)
    text = (paths.logs_dir() / "agentlens.log").read_text()
    assert "RuntimeError" in text and "supersecretvalue" not in text


def test_errlog_survives_unwritable_home(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("AGENTLENS_HOME", str(blocker / "sub"))
    errlog.log_error("ctx")  # must not raise
