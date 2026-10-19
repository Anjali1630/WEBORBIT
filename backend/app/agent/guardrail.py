"""
Pre-execution guardrail.

The LLM (or the deterministic planner) only ever proposes an *intent*, e.g.
"the search box" or "the first result link". This module is the untrusted
-> trusted boundary: it resolves that intent against the elements actually
present on the page right now, and refuses to let anything run unless the
target EXISTS, is VISIBLE, is ENABLED, and is UNIQUE. If it can't clear all
four bars, the executor is told to re-inspect / re-target / re-plan instead
of clicking blind.

TARGETING STRATEGY (highest confidence first — mirrors how a human/screen
reader would find the same element):

    1. test id            (data-testid / data-test / data-qa)
    2. accessible name     (aria-label / aria-labelledby)
    3. associated <label>
    4. stable attributes   (name / id)
    5. semantic text        (placeholder / visible text / title)
    6. role + tag priors    (button/link/textbox heuristics, ordinal position)

Each tier is scored far above the next tier down, so a weak match in a
high-confidence tier still outranks a strong match in a low-confidence one
— but ties *within* a tier still fall through to the ambiguity check below.
"""
from dataclasses import dataclass, field
from typing import Optional
import re

_ROLE_HINTS = {
    "search": ["search", "query", "find"],
    "email": ["email", "e-mail", "mail"],
    "password": ["password", "pass"],
    "username": ["username", "user", "login", "account"],
    "submit": ["submit", "sign in", "log in", "login", "continue", "next", "go", "search"],
    "download": ["download", "pdf", "file", "report", "export"],
    "name": ["name", "full name"],
    "message": ["message", "comment", "notes"],
}

# Big gaps between tiers so a match in a higher-confidence tier always wins,
# regardless of how strong a lower-tier text match looks.
TIER_TESTID = 100.0
TIER_ACCESSIBLE_NAME = 60.0
TIER_LABEL = 40.0
TIER_STABLE_ATTR = 22.0
TIER_TEXT = 8.0
TIER_ROLE_BUCKET = 2.0


@dataclass
class ResolvedTarget:
    woid: Optional[str]
    reason: str
    candidates: list = field(default_factory=list)  # other close matches, for diagnostics
    ok: bool = False
    tier: str = ""  # which targeting-strategy tier matched, for the execution trace


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _tokens(s: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", _norm(s))) - {""}


def _word_overlap(a: str, b: str) -> int:
    return len(_tokens(a) & _tokens(b))


def _score(el: dict, hint: str) -> tuple[float, str]:
    """Returns (score, tier_name) for how well `el` matches `hint`."""
    hint_l = _norm(hint)
    best_score = 0.0
    best_tier = ""

    def consider(value: str, tier_score: float, tier_name: str):
        nonlocal best_score, best_tier
        val = _norm(value)
        if not val:
            return
        bonus = 0.0
        if hint_l == val:
            bonus = 3.0
        elif hint_l in val or val in hint_l:
            bonus = 2.0
        bonus += _word_overlap(hint_l, val) * 0.5
        if bonus <= 0:
            return
        total = tier_score + bonus
        if total > best_score:
            best_score = total
            best_tier = tier_name

    # 1. test id — highest confidence a page can offer
    consider(el.get("testid", ""), TIER_TESTID, "testid")

    # 2. accessible name
    consider(el.get("accessible_name", "") or el.get("aria_label", ""), TIER_ACCESSIBLE_NAME, "accessible_name")

    # 3. associated <label>
    consider(el.get("label", ""), TIER_LABEL, "label")

    # 4. stable attributes (name / id)
    consider(el.get("name", ""), TIER_STABLE_ATTR, "name")
    consider(el.get("id", ""), TIER_STABLE_ATTR, "id")

    # 5. semantic text
    consider(el.get("placeholder", ""), TIER_TEXT, "placeholder")
    consider(el.get("text", ""), TIER_TEXT, "text")
    consider(el.get("title", ""), TIER_TEXT, "title")
    consider(el.get("href", ""), TIER_TEXT * 0.5, "href")

    # 6. role/tag priors + generic keyword buckets (weakest signal — only
    # used to break otherwise-empty ties, e.g. "search box" on a page with
    # no labels/placeholders at all).
    role = _norm(el.get("role", "")) or el.get("tag", "")
    if role and role in hint_l:
        if TIER_ROLE_BUCKET > best_score:
            best_score, best_tier = TIER_ROLE_BUCKET, "role"
    for bucket, keywords in _ROLE_HINTS.items():
        if bucket in hint_l:
            for field_name in ("accessible_name", "label", "placeholder", "text", "name"):
                val = _norm(el.get(field_name, ""))
                if val and any(k in val for k in keywords):
                    score = TIER_ROLE_BUCKET + 1.0
                    if score > best_score:
                        best_score, best_tier = score, f"keyword:{bucket}"

    return best_score, best_tier


def _resolve_str(elements: list[dict], hint: str, ordinal: Optional[int] = None) -> ResolvedTarget:
    visible_enabled = [e for e in elements if e.get("visible") and e.get("enabled")]
    if not visible_enabled:
        return ResolvedTarget(None, "no visible/enabled elements on page", [], False)

    pool = visible_enabled
    if ordinal is not None:
        # restrict to link-like elements for ordinal picks ("open the first result")
        links = [e for e in pool if e["tag"] == "a" and e.get("href")]
        pool = links if links else pool
        if len(pool) > ordinal:
            chosen = pool[ordinal]
            return ResolvedTarget(chosen["woid"], f"ordinal match #{ordinal + 1}", [chosen], True, tier="ordinal")
        return ResolvedTarget(None, f"fewer than {ordinal + 1} candidates for ordinal pick", pool[:5], False)

    scored = sorted(
        ((el, *_score(el, hint)) for el in pool),
        key=lambda triple: triple[1],
        reverse=True,
    )
    scored = [t for t in scored if t[1] > 0]

    if not scored:
        return ResolvedTarget(None, f"no element matches hint '{hint}'", [], False)

    best_el, best_score, best_tier = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0

    if best_score < TIER_ROLE_BUCKET:
        return ResolvedTarget(None, f"best match for '{hint}' too weak (score {best_score:.1f})",
                               [e for e, _, _ in scored[:3]], False)

    # uniqueness check: if the runner-up is suspiciously close (same tier,
    # similar strength), treat as ambiguous rather than guessing.
    if second_score > 0 and (best_score - second_score) < 0.6 and best_score < TIER_STABLE_ATTR:
        return ResolvedTarget(None, f"ambiguous match for '{hint}' ({best_score:.1f} vs {second_score:.1f})",
                               [e for e, _, _ in scored[:3]], False)

    return ResolvedTarget(best_el["woid"], f"matched '{hint}' via {best_tier} (score {best_score:.1f})",
                           [best_el], True, tier=best_tier)


# ---------------------------------------------------------------------------
# Structured targets
#
# A target may be either the original plain string ("search box") or a
# structured object:  {"role": "button", "name": "Search"}  (optional keys:
# "label", "placeholder", "text", "testid"). Both are supported; strings keep
# behaving exactly as before.
#
# For a structured target the role is a HARD filter: if the plan asks for a
# button named "Purchase" and the page only has a "Search" button, nothing
# matches and the action is blocked — it never falls back to some other
# element. An empty/missing target is also blocked (an empty string would
# otherwise substring-match everything).
# ---------------------------------------------------------------------------
_ROLE_EQUIV = {
    "textbox": {"textbox", "searchbox"},
    "searchbox": {"searchbox", "textbox"},
    "input": {"textbox", "searchbox"},
}


def target_text(target) -> str:
    """Human-readable form of a target (string or structured), for logs."""
    if isinstance(target, dict):
        parts = [str(target[k]) for k in ("role", "name", "label", "placeholder", "text", "testid") if target.get(k)]
        return " ".join(parts)
    return str(target or "")


def resolve(elements: list[dict], hint, ordinal: Optional[int] = None) -> ResolvedTarget:
    if ordinal is None and not target_text(hint).strip():
        return ResolvedTarget(None, "no target specified", [], False)
    if not isinstance(hint, dict):
        return _resolve_str(elements, str(hint), ordinal=ordinal)

    role = str(hint.get("role") or "").strip().lower()
    name = str(hint.get("name") or hint.get("label") or hint.get("placeholder") or hint.get("text") or "").strip()
    testid = str(hint.get("testid") or "").strip()
    desc = target_text(hint)

    pool = [e for e in elements if e.get("visible") and e.get("enabled")]
    if not pool:
        return ResolvedTarget(None, "no visible/enabled elements on page", [], False)

    if testid:
        pool = [e for e in pool if _norm(e.get("testid", "")) == testid.lower()]
        if not pool:
            return ResolvedTarget(None, f"no element with test id '{testid}'", [], False)
    if role:
        allowed = _ROLE_EQUIV.get(role, {role})
        pool = [e for e in pool if _norm(e.get("role", "")) in allowed]
        if not pool:
            return ResolvedTarget(None, f"no visible/enabled element with role '{role}'", [], False)
    if not name:
        if len(pool) == 1:
            return ResolvedTarget(pool[0]["woid"], f"only {role or 'testid'} match", [pool[0]], True, tier="role")
        return ResolvedTarget(None, f"ambiguous: {len(pool)} elements match '{desc}' and no name given", pool[:3], False)

    result = _resolve_str(pool, name, ordinal=ordinal)
    if not result.ok:
        result.reason = f"{result.reason} (structured target: {desc})"
    return result
