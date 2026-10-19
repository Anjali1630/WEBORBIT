"""
Evaluation metrics — computed only from real, completed runs recorded in
history.json. Nothing here is a placeholder or invented number; if there's
no history yet, every rate is reported as null rather than 0% (0% would
falsely imply runs happened and failed).
"""
from typing import Any, Optional

from . import history


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def compute_metrics() -> dict[str, Any]:
    entries = [e for e in history.list_entries() if e.get("status") != "running"]
    total_tasks = len(entries)
    successful_tasks = sum(1 for e in entries if e["status"] == "success")

    total_actions = 0
    done_actions = 0
    failed_actions = 0
    total_retries = 0
    total_replans = 0
    validation_rejections = 0
    blocked_actions = 0
    api_accelerator_used = 0
    playwright_used = 0
    llm_planned = 0
    deterministic_planned = 0
    durations = []

    for e in entries:
        if e.get("duration_s") is not None:
            durations.append(e["duration_s"])
        if e.get("plan_source") == "llm":
            llm_planned += 1
        else:
            deterministic_planned += 1
        for t in e.get("trace", []):
            total_actions += 1
            if t.get("status") == "done":
                done_actions += 1
            elif t.get("status") == "failed":
                failed_actions += 1
            total_retries += t.get("retry_count", 0)
            total_replans += t.get("replan_count", 0)
            if t.get("validation_status") == "rejected":
                validation_rejections += 1
            blocked_actions += t.get("blocked_count", 0)
            if t.get("tool") == "http_api":
                api_accelerator_used += 1
            elif t.get("tool") == "playwright":
                playwright_used += 1

    return {
        "tasks": {
            "total": total_tasks,
            "successful": successful_tasks,
            "success_rate": _rate(successful_tasks, total_tasks),
            "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        },
        "actions": {
            "total": total_actions,
            "done": done_actions,
            "failed": failed_actions,
            "success_rate": _rate(done_actions, total_actions),
            "validation_rejection_rate": _rate(validation_rejections, total_actions),
            "retry_rate": _rate(total_retries, total_actions),
            "replan_rate": _rate(total_replans, total_actions),
            "blocked_action_count": blocked_actions,
        },
        "planning": {
            "llm_planned": llm_planned,
            "deterministic_planned": deterministic_planned,
        },
        "capability_routing": {
            "api_accelerator_used": api_accelerator_used,
            "playwright_used": playwright_used,
        },
    }
