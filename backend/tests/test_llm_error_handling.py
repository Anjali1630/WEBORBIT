"""
Real unit tests for the OpenRouter integration's error handling
(planner.py: _call_openrouter / _parse_steps / plan_task). No network call
is made — httpx's MockTransport substitutes a canned response so these
tests are fast, deterministic, and don't require a real OPENROUTER_API_KEY.

This directly covers project-spec item 4: "invalid LLM output is rejected",
"rate-limit errors do not crash the application".
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.llm import planner  # noqa: E402


_RealAsyncClient = httpx.AsyncClient


def _client_with(handler):
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key-not-real")
    yield


def _patch_httpx_client(monkeypatch, handler):
    """planner._call_openrouter constructs its own httpx.AsyncClient(); we
    intercept the class itself so it always uses our mock transport."""
    def factory(*args, **kwargs):
        return _client_with(handler)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_valid_plan_is_parsed(monkeypatch):
    def handler(request):
        body = {"choices": [{"message": {"content": json.dumps([
            {"action": "goto", "target": "https://example.com", "label": "Open example"},
        ])}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert steps[0]["action"] == "goto"
    assert steps[-1]["action"] == "finish"  # auto-appended if missing


def test_rate_limit_does_not_crash_and_is_classified(monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})
    _patch_httpx_client(monkeypatch, handler)

    with pytest.raises(planner.LLMUnavailable) as exc_info:
        asyncio.run(planner.llm_plan("Open example.com"))
    assert "rate-limited" in str(exc_info.value) or "429" in str(exc_info.value)


def test_server_error_is_classified_not_raised_as_generic(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="service unavailable")
    _patch_httpx_client(monkeypatch, handler)

    with pytest.raises(planner.LLMUnavailable) as exc_info:
        asyncio.run(planner.llm_plan("Open example.com"))
    assert "503" in str(exc_info.value) or "unavailable" in str(exc_info.value)


def test_malformed_json_response_is_rejected(monkeypatch):
    def handler(request):
        body = {"choices": [{"message": {"content": "not json at all, sorry"}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    with pytest.raises(planner.LLMUnavailable):
        asyncio.run(planner.llm_plan("Open example.com"))


def test_unrecognized_actions_are_filtered_out(monkeypatch):
    def handler(request):
        body = {"choices": [{"message": {"content": json.dumps([
            {"action": "goto", "target": "https://example.com"},
            {"action": "sudo_rm_rf", "target": "/"},  # must never be accepted
        ])}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert all(s["action"] in planner.ACTIONS for s in steps)
    assert not any(s["action"] == "sudo_rm_rf" for s in steps)


def test_plan_task_falls_back_to_deterministic_on_llm_failure(monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})
    _patch_httpx_client(monkeypatch, handler)

    steps, meta = asyncio.run(planner.plan_task("Open Gmail"))
    assert meta.source == "deterministic"
    assert "rate-limited" in meta.reason or "429" in meta.reason
    assert steps[0]["action"] == "goto"  # deterministic planner still produced a usable plan


def test_timeout_does_not_crash(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("simulated timeout")
    _patch_httpx_client(monkeypatch, handler)

    steps, meta = asyncio.run(planner.plan_task("Open Gmail"))
    assert meta.source == "deterministic"
    assert "timed out" in meta.reason
    assert steps[0]["action"] == "goto"


def test_object_with_steps_key_is_parsed(monkeypatch):
    """Test 2 from the gap report: {"steps": [...]} must work, not just a
    bare array — this is also the format the system prompt now asks for."""
    def handler(request):
        body = {"choices": [{"message": {"content": json.dumps({
            "steps": [{"action": "navigate", "url": "https://example.com"}],
        })}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert steps[0]["action"] == "goto"
    assert steps[0]["target"] == "https://example.com"
    assert steps[-1]["action"] == "finish"


def test_markdown_fenced_json_is_parsed(monkeypatch):
    """Test 3: ```json ... ``` fences (optionally with prose around them)
    must be stripped, not treated as unparseable."""
    def handler(request):
        fenced = (
            "Sure, here's the plan:\n\n```json\n"
            + json.dumps({"steps": [{"action": "navigate", "url": "https://example.com"}]})
            + "\n```\n\nLet me know if you need anything else!"
        )
        body = {"choices": [{"message": {"content": fenced}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert steps[0]["action"] == "goto"
    assert steps[0]["target"] == "https://example.com"


def test_bare_array_in_markdown_fence_is_parsed(monkeypatch):
    """A fenced bare array (not wrapped in {"steps": ...}) must also work —
    the parser shouldn't assume the object form just because it's preferred."""
    def handler(request):
        fenced = "```json\n" + json.dumps([{"action": "navigate", "url": "https://example.com"}]) + "\n```"
        body = {"choices": [{"message": {"content": fenced}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert steps[0]["action"] == "goto"


def test_structured_target_survives_object_with_steps_parsing(monkeypatch):
    """Test 6: a {"role": ..., "name": ...} target inside {"steps": [...]}
    must come out the other side unchanged — this is the exact shape a
    real "click the Search button" plan would use."""
    def handler(request):
        body = {"choices": [{"message": {"content": json.dumps({
            "steps": [
                {"action": "navigate", "url": "https://www.youtube.com"},
                {"action": "fill", "target": {"role": "textbox", "name": "Search"}, "text": "C-drama recommendations"},
                {"action": "press", "key": "Enter"},
            ],
        })}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open YouTube and search for C-drama recommendations"))
    assert steps[0] == {"action": "goto", "url": "https://www.youtube.com", "target": "https://www.youtube.com"}
    fill_step = steps[1]
    assert fill_step["action"] == "fill"
    assert fill_step["target"] == {"role": "textbox", "name": "Search"}
    assert fill_step["value"] == "C-drama recommendations"
    press_step = steps[2]
    assert press_step["action"] == "press"
    assert press_step["value"] == "Enter"


def test_one_controlled_retry_on_malformed_response_then_succeeds(monkeypatch):
    """If the first response is unparseable but the retry (with a stricter
    instruction) comes back clean, the plan should succeed via that retry
    rather than falling back to the deterministic planner."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            body = {"choices": [{"message": {"content": "sorry, here's your plan: do stuff"}}]}
        else:
            body = {"choices": [{"message": {"content": json.dumps(
                {"steps": [{"action": "navigate", "url": "https://example.com"}]}
            )}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    steps = asyncio.run(planner.llm_plan("Open example.com"))
    assert len(calls) == 2  # exactly one retry, not zero and not more
    assert steps[0]["action"] == "goto"


def test_retry_is_bounded_to_exactly_one_attempt(monkeypatch):
    """If BOTH the original and the retry response are unparseable, the
    function must raise (so plan_task can fall back to deterministic) —
    it must NOT keep retrying indefinitely."""
    calls = []

    def handler(request):
        calls.append(1)
        body = {"choices": [{"message": {"content": "still not json, sorry"}}]}
        return httpx.Response(200, json=body)
    _patch_httpx_client(monkeypatch, handler)

    with pytest.raises(planner.LLMUnavailable):
        asyncio.run(planner.llm_plan("Open example.com"))
    assert len(calls) == 2  # original attempt + exactly one retry, no more


def test_replan_returns_none_on_failure_rather_than_raising(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="boom")
    _patch_httpx_client(monkeypatch, handler)

    result = asyncio.run(planner.replan_on_failure("task", "URL: x\nINTERACTIVE ELEMENTS:", "not found"))
    assert result is None
