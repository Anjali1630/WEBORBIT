"""
Real unit tests for agent/guardrail.py — the exists/visible/enabled/unique
gate and the priority-ordered targeting strategy (testid > accessible name >
label > name/id > text). No mocks of the guardrail itself: these are plain
function calls against hand-built DOM element lists.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import guardrail  # noqa: E402


def make_el(**overrides):
    base = {
        "woid": "w1", "tag": "input", "type": "text", "role": "textbox",
        "testid": "", "accessible_name": "", "label": "", "name": "", "id": "",
        "placeholder": "", "aria_label": "", "title": "", "text": "", "href": "",
        "visible": True, "enabled": True, "rect": {"x": 0, "y": 0, "w": 10, "h": 10},
    }
    base.update(overrides)
    return base


def test_no_elements_returns_not_ok():
    result = guardrail.resolve([], "search box")
    assert result.ok is False
    assert "no visible" in result.reason


def test_invisible_elements_are_excluded():
    els = [make_el(woid="w1", visible=False, placeholder="search")]
    result = guardrail.resolve(els, "search box")
    assert result.ok is False


def test_disabled_elements_are_excluded():
    els = [make_el(woid="w1", enabled=False, placeholder="search")]
    result = guardrail.resolve(els, "search box")
    assert result.ok is False


def test_testid_beats_everything_else():
    els = [
        make_el(woid="strong", testid="search-input"),
        make_el(woid="weak", placeholder="search box here, exact text match search box"),
    ]
    result = guardrail.resolve(els, "search box")
    assert result.ok is True
    assert result.woid == "strong"
    assert result.tier == "testid"


def test_accessible_name_beats_placeholder():
    els = [
        make_el(woid="a", accessible_name="search box"),
        make_el(woid="b", placeholder="search box"),
    ]
    result = guardrail.resolve(els, "search box")
    assert result.ok is True
    assert result.woid == "a"
    assert result.tier == "accessible_name"


def test_label_beats_name_attribute():
    els = [
        make_el(woid="a", label="Email address"),
        make_el(woid="b", name="email"),
    ]
    result = guardrail.resolve(els, "email field")
    assert result.ok is True
    assert result.woid == "a"


def test_ambiguous_targets_are_rejected_not_guessed():
    els = [
        make_el(woid="a", placeholder="name"),
        make_el(woid="b", placeholder="name"),
    ]
    result = guardrail.resolve(els, "name field")
    assert result.ok is False
    assert "ambiguous" in result.reason


def test_weak_score_is_rejected():
    els = [make_el(woid="a", text="completely unrelated content")]
    result = guardrail.resolve(els, "download button")
    assert result.ok is False


def test_ordinal_pick_selects_nth_link():
    els = [
        make_el(woid="l0", tag="a", href="/first", text="First result"),
        make_el(woid="l1", tag="a", href="/second", text="Second result"),
    ]
    result = guardrail.resolve(els, "first result", ordinal=0)
    assert result.ok is True
    assert result.woid == "l0"

    result2 = guardrail.resolve(els, "second result", ordinal=1)
    assert result2.ok is True
    assert result2.woid == "l1"


def test_ordinal_out_of_range_is_rejected():
    els = [make_el(woid="l0", tag="a", href="/only")]
    result = guardrail.resolve(els, "third result", ordinal=2)
    assert result.ok is False


# ---- structured targets ({"role": ..., "name": ...}) ----------------------

def test_structured_target_resolves_by_role_and_name():
    els = [
        make_el(woid="btn", tag="button", role="button", text="Search"),
        make_el(woid="inp", tag="input", role="textbox", placeholder="Search"),
    ]
    result = guardrail.resolve(els, {"role": "button", "name": "Search"})
    assert result.ok is True
    assert result.woid == "btn"


def test_structured_target_role_is_a_hard_filter():
    """A button named 'Search' must not match a textbox that also happens to
    say 'Search' — the wrong role must never be picked, only blocked or
    left to retry/re-plan."""
    els = [make_el(woid="inp", tag="input", role="textbox", placeholder="Search")]
    result = guardrail.resolve(els, {"role": "button", "name": "Search"})
    assert result.ok is False
    assert result.woid is None


def test_structured_target_missing_element_is_blocked_not_guessed():
    els = [make_el(woid="a", role="button", text="Cancel")]
    result = guardrail.resolve(els, {"role": "button", "name": "Purchase"})
    assert result.ok is False


def test_string_target_backward_compatible_with_structured_resolver():
    """Plain strings must keep working exactly as before — same function,
    same behavior, no regression from adding structured-target support."""
    els = [make_el(woid="a", placeholder="search box")]
    result = guardrail.resolve(els, "search box")
    assert result.ok is True
    assert result.woid == "a"


def test_empty_target_is_blocked():
    els = [make_el(woid="a", placeholder="anything")]
    result = guardrail.resolve(els, "")
    assert result.ok is False
    result2 = guardrail.resolve(els, {})
    assert result2.ok is False


def test_target_text_helper_handles_both_forms():
    assert guardrail.target_text("search box") == "search box"
    assert guardrail.target_text({"role": "button", "name": "Search"}) == "button Search"
