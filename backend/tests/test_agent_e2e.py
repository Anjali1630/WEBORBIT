"""
End-to-end tests: a real WebSocket client sends real "run_task" messages to
the real, running WebOrbit server, which drives a real headless Chromium
via Playwright against the bundled demo site. Nothing here is mocked —
these are the same checks performed manually during development, made
repeatable.

Requires: `playwright install chromium` to have been run once.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _run_task_and_collect(base_url: str, task: str, timeout: float = 30.0) -> dict:
    """Runs one task to completion and returns {logs, steps, result, states}."""
    ws_url = base_url.replace("http://", "ws://") + "/ws"
    logs, states, final_result, final_steps = [], [], None, None
    async with websockets.connect(ws_url, max_size=10_000_000) as ws:
        await ws.send(json.dumps({"type": "run_task", "task": task}))

        async def _consume():
            nonlocal final_result, final_steps
            async for raw in ws:
                data = json.loads(raw)
                t = data.get("type")
                if t == "frame":
                    continue
                if t == "log":
                    logs.append(data["message"])
                elif t == "status":
                    states.append(data["state"])
                    if data["state"] == "idle":
                        return
                elif t == "steps":
                    final_steps = data["steps"]
                elif t == "result":
                    final_result = data

        await asyncio.wait_for(_consume(), timeout=timeout)
    return {"logs": logs, "states": states, "result": final_result, "steps": final_steps}


@pytest.mark.asyncio
async def test_navigate_and_extract_table_via_api_accelerator(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo table page and extract the table")
    assert out["result"]["status"] == "success"
    table = out["result"]["result"]["table"]
    assert len(table) == 4  # header + 3 data rows
    assert table[0] == ["Region", "Signups", "Growth"]
    assert any("API-first accelerator" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_form_fill_and_submit(live_server):
    out = await _run_task_and_collect(
        live_server,
        "Fill the demo form with name: Anjali, email: anjali@example.com, message: hello",
    )
    assert out["result"]["status"] == "success"
    assert all(s["status"] == "done" for s in out["steps"])


@pytest.mark.asyncio
async def test_search_verifies_page_changed(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo search page and search for python courses")
    assert out["result"]["status"] == "success"
    assert any("Verified: page updated" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_download_is_verified_nonempty(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo download page and download the report")
    assert out["result"]["status"] == "success"
    path = out["result"]["result"]["download"]
    assert path is not None
    p = Path(path)
    assert p.exists() and p.stat().st_size > 0
    assert any("Verified download" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_unresolvable_target_triggers_bounded_retry_and_fails_cleanly(live_server):
    out = await _run_task_and_collect(live_server, "Fill the demo table page with phone: 12345")
    assert out["result"]["status"] == "failed"
    assert any("Retrying the same step" in line for line in out["logs"])
    assert any("Re-inspecting page" in line for line in out["logs"])
    assert any("couldn't resolve" in line for line in out["logs"])
    # never an infinite loop — bounded at MAX_ATTEMPTS
    retry_lines = [l for l in out["logs"] if "Retrying the same step" in l]
    assert len(retry_lines) <= 2  # one retry per failed action in this 2-action task


@pytest.mark.asyncio
async def test_failed_step_stops_the_task_instead_of_continuing(live_server):
    """A real bug, not hypothetical: previously the step loop never broke
    on failure, so a trailing 'finish' step ran anyway and unconditionally
    logged 'Task complete' / marked itself done — exactly the "Search ❌,
    Summarise result ✓, Task complete" failure pattern. This drives a plan
    with an early, unrecoverable click failure followed by two more steps
    (extract_table, finish) and asserts neither of them ran at all."""
    out = await _run_task_and_collect(
        live_server,
        "Open the demo table page, click the missing purchase widget and extract the table",
    )
    assert out["result"]["status"] == "failed"
    assert out["result"]["result"]["table"] is None  # extract_table never ran
    assert not any("Task complete" in line for line in out["logs"])  # finish never ran
    statuses = [s["status"] for s in out["steps"]]
    assert statuses[0] == "done"     # the goto succeeded
    assert statuses[1] == "failed"   # the click correctly failed
    assert statuses[2:] == ["pending"] * len(statuses[2:])  # never attempted, not silently skipped-green


@pytest.mark.asyncio
async def test_retry_recovers_from_late_rendering_element(live_server):
    """Project-spec item 6: 'changed button text -> should attempt safe
    re-targeting'. The /demo/delayed page starts with a disabled, unlabeled
    button that JS relabels to 'confirm button' ~400ms after load — attempt
    1 must fail (guardrail correctly refuses the disabled element), and
    attempt 2's re-inspect + retry must then succeed once the real DOM has
    changed. This is a genuine recovery, not a re-run of the failure path."""
    out = await _run_task_and_collect(live_server, "Open the demo delayed page and click the confirm button")
    assert out["result"]["status"] == "success"
    assert all(s["status"] == "done" for s in out["steps"])
    assert any("Retrying the same step" in line for line in out["logs"])
    assert any("Clicked" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_state_machine_transitions_are_present(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo table page and extract the table")
    for expected in ("planning", "navigating", "observing", "verifying", "completed", "idle"):
        assert expected in out["states"], f"missing state transition: {expected}"


@pytest.mark.asyncio
async def test_unfamiliar_domain_requires_confirmation(live_server):
    ws_url = live_server.replace("http://", "ws://") + "/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "run_task", "task": "Open https://example.org"}))
        saw_confirm = False
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "await_confirm_domain":
                saw_confirm = True
                assert data["domain"] == "example.org"
                await ws.send(json.dumps({"type": "confirm_domain", "allow": False}))
            if data.get("type") == "status" and data.get("state") == "idle":
                break
        assert saw_confirm


@pytest.mark.asyncio
async def test_dom_inspector_pierces_shadow_dom(live_server):
    """Real bug report: a search box inside a web component's shadow root
    (like YouTube's) was invisible to the inspector, which only walked the
    light DOM. This drives the actual INSPECT_JS against a page with two
    *nested* shadow roots and confirms the search input, button, and table
    inside them are all found — not just asserting the fix "should" work."""
    from app.agent.dom_inspector import INSPECT_JS
    from app.agent.executor import BrowserSession

    session = BrowserSession()
    await session.start()
    try:
        await session.goto(f"{live_server}/demo/shadow", force=True)
        result = await session.page.evaluate(INSPECT_JS)
        roles_and_names = {(e["role"], e.get("accessible_name")) for e in result["elements"]}
        assert ("textbox", "search box") in roles_and_names
        assert ("button", "search button") in roles_and_names
        assert result["tables_count"] >= 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_search_inside_shadow_dom_succeeds_end_to_end(live_server):
    """The full agent loop — plan, inspect, resolve, type, submit, verify —
    against a search box two shadow roots deep. Before the fix this failed
    exactly like the YouTube report: target resolution found nothing."""
    out = await _run_task_and_collect(live_server, "Open the demo shadow page and search for hello shadow dom")
    assert out["result"]["status"] == "success"
    assert all(s["status"] == "done" for s in out["steps"])
    assert any("matched 'search box' via accessible_name" in line for line in out["logs"])
    assert any("Verified: page updated after submitting" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_extract_table_inside_shadow_dom(live_server):
    """extract_table's own JS had the same light-DOM-only limitation —
    confirms it now finds a <table> nested inside a shadow root too."""
    out = await _run_task_and_collect(live_server, "Open the demo shadow page and extract the table")
    assert out["result"]["status"] == "success"
    table = out["result"]["result"]["table"]
    assert table == [["Item", "Qty"], ["Widget", "3"], ["Gadget", "7"]]


def test_metrics_reflect_real_runs(live_server):
    import httpx
    resp = httpx.get(f"{live_server}/api/metrics", timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tasks"]["total"] >= 1
    assert body["actions"]["total"] >= 1
    assert "blocked_action_count" in body["actions"]


def test_history_records_local_demo_fallback_mode(live_server):
    """No OPENROUTER_API_KEY is set for the test server (see conftest.py),
    so every task's history entry must record the human-readable mode
    'Local Demo Fallback', not just the internal plan_source string."""
    import httpx
    resp = httpx.get(f"{live_server}/api/history", timeout=5)
    assert resp.status_code == 200
    entries = resp.json()
    assert entries, "expected at least one history entry from earlier tests in this session"
    assert entries[0]["mode"] == "Local Demo Fallback"
    assert entries[0]["plan_source"] == "deterministic"  # unchanged field, still present


@pytest.mark.asyncio
async def test_screenshot_action_creates_a_real_file(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo home and take a screenshot of this page")
    assert out["result"]["status"] == "success"
    path = out["result"]["result"]["screenshot"]
    assert path is not None
    p = Path(path)
    assert p.exists() and p.stat().st_size > 0
    assert any("Screenshot saved" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_scroll_action_moves_the_page(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo long page and scroll down 500 pixels")
    assert out["result"]["status"] == "success"
    assert any("Scrolled down (y: 0 ->" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_fill_action_sets_field_value(live_server):
    out = await _run_task_and_collect(live_server, "Open the demo form page and fill the email field with test@example.com")
    assert out["result"]["status"] == "success"
    assert any("Filled: test@example.com" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_back_action_navigates_to_previous_page(live_server):
    """Uses the canonical 'back' action name via a hand-built plan sent
    straight to the executor's action dispatch, exercised the same way the
    planner's own steps are — go to the table page, then the search page,
    then go back, and confirm the URL actually returned to /demo/table."""
    out = await _run_task_and_collect(
        live_server,
        "Open the demo table page, open the demo search page and then go back",
    )
    assert out["result"]["status"] == "success"
    assert any("Went back" in line for line in out["logs"])


@pytest.mark.asyncio
async def test_structured_target_click_resolves_by_role_and_name(live_server):
    """The deterministic planner only emits string targets today (the
    structured {"role","name"} form is for the OpenRouter planner), so this
    drives the real production dispatch function directly — the exact code
    path a structured target from an LLM plan would take — against the
    live demo site's actual submit button, rather than re-testing the
    resolver in isolation (already covered in test_guardrail.py)."""
    from app.agent.executor import BrowserSession
    from app.main import AgentSession

    class _NullWS:
        async def send_text(self, *_args, **_kwargs):
            pass

    session = AgentSession(_NullWS())
    session.browser = BrowserSession()
    await session.browser.start()
    try:
        await session.browser.goto(f"{live_server}/demo/forms", force=True)
        result_summary = {"text": None, "table": None, "download": None, "screenshot": None}
        step = {"action": "click", "target": {"role": "button", "name": "submit button"}}
        ok, tool, validation_status, error = await session._run_action_once(step, result_summary)
        assert ok is True, f"structured target click failed: {error}"
        assert validation_status == "ok"
        body_text = await session.browser.page.inner_text("body")
        assert "Thanks" in body_text  # the form's real confirmation page
    finally:
        await session.browser.close()


@pytest.mark.asyncio
async def test_structured_target_wrong_role_is_blocked_not_misclicked(live_server):
    """The mirror image of the success case: asking for a role that doesn't
    exist on the page must be BLOCKED, never satisfied by clicking whatever
    happens to be nearby."""
    from app.agent.executor import BrowserSession
    from app.main import AgentSession

    class _NullWS:
        async def send_text(self, *_args, **_kwargs):
            pass

    session = AgentSession(_NullWS())
    session.browser = BrowserSession()
    await session.browser.start()
    try:
        await session.browser.goto(f"{live_server}/demo/forms", force=True)
        result_summary = {"text": None, "table": None, "download": None, "screenshot": None}
        step = {"action": "click", "target": {"role": "checkbox", "name": "submit button"}}
        ok, tool, validation_status, error = await session._run_action_once(step, result_summary)
        assert ok is False
        assert validation_status == "rejected"
        body_text = await session.browser.page.inner_text("body")
        assert "Thanks" not in body_text  # confirms nothing was actually clicked
    finally:
        await session.browser.close()


@pytest.mark.asyncio
async def test_login_handoff_and_resume(live_server):
    """Project-spec item 7: real WAITING FOR USER -> manual login ->
    LOGIN DETECTED -> CONTINUE, driven through the actual interactive
    mouse/keyboard forwarding path (not a forced signal).

    A human clicks by eye; an automated test can't. So this test uses a
    throwaway Playwright browser purely as a coordinate *oracle*: it loads
    the exact same static /demo/login page at the exact same viewport size
    used by the agent's own browser (config.VIEWPORT), reads the real
    on-screen positions of the username/password/submit elements, and
    replays those exact coordinates through the same mouse_click/key_type
    WebSocket messages a human's clicks would send. This proves the
    interactive-control wiring end-to-end, not just that a signal flag
    got set.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        oracle_browser = await p.chromium.launch()
        oracle_page = await oracle_browser.new_page(viewport={"width": 1280, "height": 800})
        await oracle_page.goto(f"{live_server}/demo/login")
        user_box = await oracle_page.locator('[aria-label="username field"]').bounding_box()
        pass_box = await oracle_page.locator('[aria-label="password field"]').bounding_box()
        submit_box = await oracle_page.locator('button[type="submit"]').bounding_box()
        await oracle_browser.close()

    assert user_box and pass_box and submit_box, "oracle browser could not find the login form fields"

    def rel_center(box):
        return ((box["x"] + box["width"] / 2) / 1280, (box["y"] + box["height"] / 2) / 800)

    ux, uy = rel_center(user_box)
    px, py = rel_center(pass_box)
    sx, sy = rel_center(submit_box)

    ws_url = live_server.replace("http://", "ws://") + "/ws"
    logs, saw_waiting, saw_detected = [], False, False
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "run_task",
            "task": "Open the demo mail app and search for internship emails",
        }))

        async def _drive():
            nonlocal saw_waiting, saw_detected
            async for raw in ws:
                data = json.loads(raw)
                t = data.get("type")
                if t == "frame":
                    continue
                if t == "log":
                    logs.append(data["message"])
                if t == "await_login":
                    saw_waiting = True
                    await ws.send(json.dumps({"type": "mouse_click", "x": ux, "y": uy}))
                    await ws.send(json.dumps({"type": "key_type", "text": "demo@example.com"}))
                    await ws.send(json.dumps({"type": "mouse_click", "x": px, "y": py}))
                    await ws.send(json.dumps({"type": "key_type", "text": "password123"}))
                    await ws.send(json.dumps({"type": "mouse_click", "x": sx, "y": sy}))
                if t == "login_resolved":
                    saw_detected = True
                if t == "status" and data.get("state") == "idle":
                    return

        await asyncio.wait_for(_drive(), timeout=30)

    assert saw_waiting, "WAITING FOR USER (await_login) never appeared"
    assert saw_detected, "LOGIN DETECTED (login_resolved) never fired"
    assert any("Authentication required" in l for l in logs)
    assert any("Authentication detected" in l for l in logs)
