from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlens.adapters import claude_code as cc
from agentlens.events import Category, Event, Provenance, TsSource, tool_event_id

FIXDIR = Path(__file__).parent / "fixtures" / "claude_code"
ALL_FIXTURES = sorted(p.stem for p in FIXDIR.glob("*.json"))
TS = 1_763_000_000.25
OFF = {"capture_prompts": False}


def norm(fixture, name, cfg=OFF, ts=TS) -> Event:
    (ev,) = cc.normalize(fixture(name), ts, cfg)
    return ev


def dumped(ev: Event) -> str:
    return json.dumps(ev.to_dict())


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_normalizes_and_round_trips(fixture, name):
    ev = norm(fixture, name)
    assert ev.provider == "claude-code"
    assert ev.session_id == "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
    assert ev.project == "/Users/dev/shop"
    assert ev.ts == TS and ev.ts_source is TsSource.COLLECTOR
    assert ev.provenance is Provenance.OBSERVED
    assert Event.from_dict(json.loads(dumped(ev))) == ev


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_private_content_survives(fixture, name):
    out = dumped(norm(fixture, name))
    for forbidden in [
        "hunter2-fixture",  # prompt text
        "Fix the failing checkout",  # prompt text
        "fixture-not-a-secret-123",  # KEY=value in a command
        "fixture-file-content-secret",  # Write content
        "SECRET_FILE_BODY",  # Read output
        "3 passed in",  # Bash stdout
        "cart total is computed",  # assistant message
        "All 3 checkout tests",  # assistant message
        "fixturepw",  # URL userinfo
        "fixture-qs-token",  # query-string token
        "fixture-token-value",  # secret-named MCP arg
        "keep the cart details",  # compact instructions
        "find every place",  # subagent prompt
    ]:
        assert forbidden not in out, (name, forbidden)


def test_session_start(fixture):
    ev = norm(fixture, "session_start")
    assert (ev.category, ev.type) == (Category.SESSION, "session.start")
    assert ev.data["start_source"] == "startup"
    assert ev.data["model"] == "claude-sonnet-5-5"
    assert ev.data["permission_mode"] == "default"


def test_session_start_without_model_does_not_invent_one(fixture):
    ev = norm(fixture, "session_start_clear")
    assert "model" not in ev.data and ev.data["start_source"] == "clear"


def test_session_end(fixture):
    ev = norm(fixture, "session_end")
    assert ev.type == "session.end" and ev.data["end_reason"] == "prompt_input_exit"


def test_prompt_default_stores_length_and_keyed_hash_only(fixture):
    payload = fixture("user_prompt_submit")
    ev = norm(fixture, "user_prompt_submit")
    assert ev.category is Category.PROMPT
    assert ev.data["prompt_chars"] == len(payload["prompt"])
    assert len(ev.data["prompt_hmac"]) == 64
    assert "prompt_text" not in ev.data


def test_prompt_capture_opt_in_is_redacted(fixture):
    payload = fixture("user_prompt_submit")
    payload["prompt"] += " token: " + "gh" + "p_" + "Z" * 36
    (ev,) = cc.normalize(payload, TS, {"capture_prompts": True})
    assert "Fix the failing checkout test" in ev.data["prompt_text"]
    assert "Z" * 36 not in ev.data["prompt_text"]


def test_bash_pre(fixture):
    ev = norm(fixture, "pre_tool_use_bash")
    assert (ev.category, ev.type, ev.status) == (Category.TOOL, "tool.pre", "started")
    assert ev.tool_name == "Bash" and ev.tool_use_id == "toolu_01BashAAA"
    cmd = ev.data["input"]["command"]
    assert cmd.startswith("API_KEY=[REDACTED:value] pytest -q")
    assert ev.data["input"]["description"] == "Run checkout tests"
    assert ev.data["effort"] == "high"
    assert ev.event_id == tool_event_id("claude-code", ev.session_id, "PreToolUse", "toolu_01BashAAA")


def test_bash_command_truncated_to_500():
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_use_id": "t", "tool_input": {"command": "echo " + "x" * 2000}}
    (ev,) = cc.normalize(payload, TS, OFF)
    assert len(ev.data["input"]["command"]) <= 501


def test_bash_post_observed_duration_and_response_shape(fixture):
    ev = norm(fixture, "post_tool_use_bash")
    assert (ev.type, ev.status, ev.duration_ms) == ("tool.post", "ok", 1840)
    assert ev.data["response"] == {"stdout_chars": 18, "stderr_chars": 0, "interrupted": False, "isImage": False}


def test_post_without_duration_leaves_it_unavailable(fixture):
    ev = norm(fixture, "post_tool_use_write")
    assert ev.duration_ms is None


def test_bash_failure_exit_code(fixture):
    ev = norm(fixture, "post_tool_use_failure_bash")
    assert (ev.type, ev.status) == ("tool.failure", "failed")
    assert ev.data["exit_code"] == 2
    assert ev.data["error"].startswith("Exit code 2\nsrc/cart.ts")


def test_interrupt(fixture):
    ev = norm(fixture, "post_tool_use_failure_interrupt")
    assert ev.status == "interrupted" and ev.duration_ms == 3021
    assert "exit_code" not in ev.data


def test_write_stores_sizes_not_content(fixture):
    ev = norm(fixture, "pre_tool_use_write")
    assert ev.data["input"] == {"file_path": "/Users/dev/shop/.env", "content_size": {"bytes": 50, "lines": 2}}


def test_edit_stores_lengths(fixture):
    ev = norm(fixture, "pre_tool_use_edit")
    assert ev.data["input"] == {
        "file_path": "/Users/dev/shop/src/cart.ts",
        "old_string_chars": 25, "new_string_chars": 23, "replace_all": False,
    }


def test_read(fixture):
    ev = norm(fixture, "pre_tool_use_read")
    assert ev.data["input"] == {"file_path": "/Users/dev/shop/src/cart.ts", "offset": 1, "limit": 200}
    post = norm(fixture, "post_tool_use_read")
    assert post.data["response"] == {"type_chars": 4, "file_keys": 3}


def test_mcp_generic_sanitizer(fixture):
    ev = norm(fixture, "pre_tool_use_mcp")
    inp = ev.data["input"]
    assert inp["owner"] == "dev" and inp["title"] == "Checkout bug"
    assert inp["body"] == {"chars": len("Long body " * 40)}
    assert inp["labels"] == {"items": 1}
    assert inp["token"] == "[REDACTED:value]"
    assert ev.data["mcp_server"] == "github"


def test_webfetch_url_redacted(fixture):
    ev = norm(fixture, "pre_tool_use_webfetch")
    url = ev.data["input"]["url"]
    assert url.startswith("https://[REDACTED:userinfo]@internal.example.com/api?access_token=[REDACTED:value]")
    assert "page=2" in url
    assert ev.data["input"]["prompt_chars"] == len("Summarize the API")


def test_agent_tool(fixture):
    ev = norm(fixture, "pre_tool_use_agent")
    assert ev.data["input"]["subagent_type"] == "Explore"
    assert "prompt_chars" in ev.data["input"]


def test_permission_request_has_no_tool_use_id(fixture):
    ev = norm(fixture, "permission_request")
    assert (ev.category, ev.type, ev.status) == (Category.APPROVAL, "approval.request", "requested")
    assert ev.tool_use_id is None
    assert ev.data["input"]["command"] == "rm -rf build"


def test_permission_denied_links_tool(fixture):
    ev = norm(fixture, "permission_denied")
    assert (ev.type, ev.status, ev.tool_use_id) == ("approval.denied", "denied", "toolu_01BashDDD")
    assert ev.data["reason"] == "Force push to protected branch"


def test_notification(fixture):
    ev = norm(fixture, "notification")
    assert ev.data["notification_type"] == "permission_prompt"
    assert ev.data["title"] == "Permission needed"


def test_subagents(fixture):
    start = norm(fixture, "subagent_start")
    stop = norm(fixture, "subagent_stop")
    assert (start.category, start.type) == (Category.TASK, "subagent.start")
    assert stop.data["agent_id"] == "agent-7c1d" and stop.data["agent_type"] == "Explore"
    assert stop.data["last_assistant_message_chars"] > 0


def test_stop_and_failure(fixture):
    stop = norm(fixture, "stop")
    assert stop.type == "turn.stop" and stop.data["stop_hook_active"] is False
    fail = norm(fixture, "stop_failure")
    assert (fail.category, fail.type, fail.status) == (Category.ERROR, "turn.failure", "failed")
    assert fail.data["error"] == "rate_limit"


def test_pre_compact(fixture):
    ev = norm(fixture, "pre_compact")
    assert ev.data["trigger"] == "manual" and ev.data["custom_instructions_chars"] == 21


def test_unknown_event_kept_minimal(fixture):
    ev = norm(fixture, "unknown_event")
    assert ev.type == "claude_code.SomeFutureEvent"
    assert "whatever" not in json.dumps(ev.data)


def test_duplicate_firing_same_id(fixture):
    a = norm(fixture, "stop", ts=TS)
    b = norm(fixture, "stop", ts=TS + 0.5)
    assert a.event_id == b.event_id
    t1 = norm(fixture, "pre_tool_use_bash", ts=TS)
    t2 = norm(fixture, "pre_tool_use_bash", ts=TS + 30)
    assert t1.event_id == t2.event_id


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", {}, {"session_id": "s"}, {"hook_event_name": "Stop"}, {"session_id": 5, "hook_event_name": "Stop"}],
)
def test_invalid_payloads_raise(payload):
    with pytest.raises(ValueError):
        cc.normalize(payload, TS, OFF)


def test_wrong_types_ignored_not_crashing():
    payload = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": 7,
               "tool_use_id": ["x"], "duration_ms": "fast", "tool_response": 3.5, "cwd": None}
    (ev,) = cc.normalize(payload, TS, OFF)
    assert ev.tool_name is None and ev.tool_use_id is None and ev.duration_ms is None


def test_hook_events_registered_match_map():
    assert set(cc.HOOK_EVENTS) == set(cc.EVENT_MAP)
    assert "MessageDisplay" not in cc.HOOK_EVENTS
