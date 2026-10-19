"""
Real unit tests for app/metrics.py, using a monkeypatched history so the
distinction between "failed for some other reason" and "blocked by the
guardrail before it ever ran" is exact and doesn't depend on which demo
tasks happened to run earlier in the session.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import history, metrics  # noqa: E402


def _entry(trace):
    return {
        "status": "success", "duration_s": 1.0, "plan_source": "deterministic",
        "trace": trace,
    }


def test_blocked_action_count_only_counts_guardrail_blocks(monkeypatch):
    fake_history = [
        _entry([
            # a guardrail-blocked action: rejected target, retried, still blocked
            {"status": "failed", "validation_status": "rejected", "tool": "playwright",
             "retry_count": 2, "replan_count": 0, "blocked_count": 1},
            # a successful action — never blocked
            {"status": "done", "validation_status": "ok", "tool": "playwright",
             "retry_count": 0, "replan_count": 0, "blocked_count": 0},
            # a failure that is NOT a guardrail block (e.g. an empty table,
            # a failed download verification) — must not count as blocked
            {"status": "failed", "validation_status": "n/a", "tool": "playwright",
             "retry_count": 0, "replan_count": 0, "blocked_count": 0},
        ]),
    ]
    monkeypatch.setattr(history, "list_entries", lambda: fake_history)

    result = metrics.compute_metrics()
    assert result["actions"]["total"] == 3
    assert result["actions"]["failed"] == 2
    assert result["actions"]["blocked_action_count"] == 1


def test_blocked_action_count_is_zero_when_nothing_was_blocked(monkeypatch):
    fake_history = [_entry([
        {"status": "done", "validation_status": "ok", "tool": "playwright",
         "retry_count": 0, "replan_count": 0, "blocked_count": 0},
    ])]
    monkeypatch.setattr(history, "list_entries", lambda: fake_history)

    result = metrics.compute_metrics()
    assert result["actions"]["blocked_action_count"] == 0


def test_blocked_action_count_accumulates_across_tasks(monkeypatch):
    fake_history = [
        _entry([{"status": "failed", "validation_status": "rejected", "tool": "playwright",
                 "retry_count": 1, "replan_count": 0, "blocked_count": 1}]),
        _entry([{"status": "failed", "validation_status": "rejected", "tool": "playwright",
                 "retry_count": 1, "replan_count": 0, "blocked_count": 1}]),
    ]
    monkeypatch.setattr(history, "list_entries", lambda: fake_history)

    result = metrics.compute_metrics()
    assert result["actions"]["blocked_action_count"] == 2


def test_metrics_backward_compatible_with_entries_missing_blocked_count(monkeypatch):
    """History entries written before this change won't have blocked_count
    in their trace at all — must not raise, must default to 0 contribution."""
    fake_history = [_entry([
        {"status": "done", "validation_status": "ok", "tool": "playwright",
         "retry_count": 0, "replan_count": 0},  # no "blocked_count" key
    ])]
    monkeypatch.setattr(history, "list_entries", lambda: fake_history)

    result = metrics.compute_metrics()
    assert result["actions"]["blocked_action_count"] == 0
