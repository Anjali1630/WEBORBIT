"""
Real unit tests for the deterministic planner — no LLM involved, pure
regex/keyword logic. These lock in the phrasing patterns called out
explicitly in the project brief.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import planner  # noqa: E402


def actions(steps):
    return [s["action"] for s in steps]


def test_simple_open_site():
    steps = planner.deterministic_plan("Open Gmail.")
    assert actions(steps) == ["goto", "finish"]
    assert steps[0]["target"] == "https://mail.google.com/"


def test_open_and_search():
    steps = planner.deterministic_plan("Open Gmail and search for emails from my college")
    assert actions(steps) == ["goto", "type", "finish"]
    assert steps[1]["value"] == "emails from my college"
    assert steps[1]["submit"] is True


def test_search_demo_site():
    steps = planner.deterministic_plan("Open the demo search page and search for python courses")
    assert actions(steps) == ["goto", "type", "finish"]
    assert steps[0]["target"] == "/demo/search"
    assert steps[1]["value"] == "python courses"


def test_fill_form_with_fields():
    steps = planner.deterministic_plan(
        "Fill the demo form with name: Anjali, email: anjali@example.com, message: hello"
    )
    assert actions(steps) == ["goto", "type", "type", "type", "click", "finish"]
    assert steps[0]["target"] == "/demo/forms"
    values = {s["target"]: s["value"] for s in steps if s["action"] == "type"}
    assert values["name field"] == "Anjali"
    assert values["email field"] == "anjali@example.com"
    assert values["message field"] == "hello"


def test_extract_table():
    steps = planner.deterministic_plan("Open the demo table page and extract the table")
    assert actions(steps) == ["goto", "extract_table", "finish"]


def test_download_task():
    steps = planner.deterministic_plan("Open the demo download page and download the report")
    assert actions(steps) == ["goto", "download", "finish"]
    # regression: a site alias containing the word "download" must not be
    # misread as a download *action* on the navigation clause itself
    assert steps[0]["action"] == "goto"
    assert steps[0]["target"] == "/demo/download"


def test_ordinal_open_first_result():
    steps = planner.deterministic_plan("Open the demo search page and open the first result")
    labels = actions(steps)
    assert "click" in labels
    click_step = next(s for s in steps if s["action"] == "click")
    assert click_step["ordinal"] == 0


def test_generic_click_on_arbitrary_hint():
    steps = planner.deterministic_plan("Open the demo delayed page and click the confirm button")
    assert actions(steps) == ["goto", "click", "finish"]
    click_step = next(s for s in steps if s["action"] == "click")
    assert click_step["target"] == "confirm button"
    assert click_step.get("ordinal") is None


def test_multi_step_chain():
    steps = planner.deterministic_plan(
        "Open the demo mail app, search for internship emails, open the latest one and tell me the sender"
    )
    assert actions(steps) == ["goto", "type", "click", "extract_text", "finish"]
    click_step = next(s for s in steps if s["action"] == "click")
    assert click_step["ordinal"] == 0  # "latest" maps to ordinal 0


def test_unmatched_task_falls_back_to_navigation():
    steps = planner.deterministic_plan("blah blah nothing recognisable")
    assert steps[-1]["action"] == "finish"
    assert steps[0]["action"] == "goto"


def test_go_back_and_press_enter():
    steps = planner.deterministic_plan("Go back")
    assert actions(steps) == ["go_back", "finish"]

    steps2 = planner.deterministic_plan("Press Enter")
    assert actions(steps2) == ["press", "finish"]
    assert steps2[0]["value"] == "Enter"


def test_screenshot_task():
    steps = planner.deterministic_plan("Take a screenshot of this page")
    assert actions(steps) == ["screenshot", "finish"]


def test_scroll_task_with_amount_and_direction():
    steps = planner.deterministic_plan("Scroll down 300 pixels")
    assert actions(steps) == ["scroll", "finish"]
    assert steps[0]["direction"] == "down"
    assert steps[0]["amount"] == 300


def test_scroll_to_find_table_also_extracts():
    steps = planner.deterministic_plan("Scroll down to find the table")
    assert actions(steps) == ["scroll", "extract_table", "finish"]


def test_single_field_fill_produces_structured_target():
    steps = planner.deterministic_plan("Fill the email field with test@example.com")
    assert actions(steps) == ["fill", "finish"]
    fill_step = steps[0]
    assert fill_step["value"] == "test@example.com"
    assert isinstance(fill_step["target"], dict)
    assert fill_step["target"]["role"] == "textbox"
    assert "email" in fill_step["target"]["name"].lower()


def test_normalize_step_maps_llm_aliases():
    step = planner.normalize_step({"action": "navigate", "url": "https://example.com"})
    assert step["action"] == "goto"
    assert step["target"] == "https://example.com"

    step2 = planner.normalize_step({"action": "fill", "target": "Email", "text": "hi@example.com"})
    assert step2["value"] == "hi@example.com"

    step3 = planner.normalize_step({"action": "click", "target": "Search"})
    assert step3 == {"action": "click", "target": "Search"}  # no-op for already-canonical steps


def test_back_and_forward_are_valid_actions():
    assert "back" in planner.ACTIONS
    assert "forward" in planner.ACTIONS
    assert "go_back" in planner.ACTIONS  # backward compatibility preserved
    assert "go_forward" in planner.ACTIONS


def test_plan_task_uses_deterministic_when_no_api_key(monkeypatch):
    import asyncio
    from app import config
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    steps, meta = asyncio.run(planner.plan_task("Open Gmail"))
    assert meta.source == "deterministic"
    assert meta.reason == "no API key configured"
    assert steps[0]["action"] == "goto"
