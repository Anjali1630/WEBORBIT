"""
Planning layer.

IMPORTANT — per the project spec, the LLM is called ONCE per task to
produce a plan (and only again, sparingly, on re-plan after a failed
step). Every other decision — which DOM element matches an intent,
whether it's safe to click, retries — is deterministic code in
agent/guardrail.py and agent/executor.py, not another model call.

If OPENROUTER_API_KEY is unset, planning falls back to a fully
deterministic, regex/keyword based parser (`deterministic_plan`) that
understands the task phrasings called out in the project brief. This
keeps the whole app usable with zero API key and zero cost.

`plan_task` returns (steps, meta) rather than just steps, so the caller
can log/trace *why* a given plan was produced — which mode was used, and
if OpenRouter was tried but failed, what kind of failure (rate limit,
timeout, bad response) sent it to the deterministic fallback. That
mirrors project-spec section 20 ("do not crash, do not fake a response,
log the real reason").
"""
import json
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .. import config

ACTIONS = {
    "goto", "click", "type", "fill", "select", "press", "scroll", "wait", "screenshot",
    "back", "forward", "go_back", "go_forward",  # go_back/go_forward kept for backward compatibility
    "extract_text", "extract_table", "download", "finish",
}
# accepted from an LLM and rewritten by normalize_step()
_ACTION_ALIASES = {"navigate": "goto", "extract": "extract_text"}

SYSTEM_PROMPT = """You are the planning module of WebOrbit, a general-purpose browser
automation agent. Given one natural-language task, return ONLY valid JSON. Do not use
Markdown. Do not include explanations. Do not include ```json fences. Return a single
JSON object of this exact shape:

{"steps": [ ... ]}

Each item in "steps" is an object with:
  action: one of "navigate" | "click" | "type" | "fill" | "select" | "press" | "scroll" |
          "wait" | "screenshot" | "back" | "forward" | "extract" | "extract_table" |
          "download" | "finish"
          ("navigate" and "extract" are accepted aliases for "goto" and "extract_text";
          "back"/"forward" are preferred, "go_back"/"go_forward" also still accepted)
  target: for "navigate" an absolute URL (a "url" field is also accepted in its place);
          for "click"/"type"/"fill"/"select"/"extract"/"download" EITHER a short
          plain-language description of the element (e.g. "search box") OR a structured
          object such as {"role": "button", "name": "Search"} or
          {"role": "textbox", "name": "Email"} (roles: button, link, textbox, searchbox,
          combobox, checkbox, radio). Prefer the structured form when you can tell the role.
  value: text to enter (type/fill), the option to choose (select), or the key to press
         (press, e.g. "Enter"); "text" and "key" are also accepted as field names for this
  direction / amount: for "scroll" — direction is "down"|"up"|"top"|"bottom", amount is
          pixels (default 600)
  path: optional plain file name for "screenshot" (e.g. "page.png")
  ordinal: 0-based position when the target says "first"/"second"/"latest" (omit otherwise)
  submit: true if a "type" step should submit (press Enter) after typing
  label: a short human-readable description of this step, shown to the user

Every step must have a valid "action". Never invent a CSS selector. Always end the
"steps" array with a "finish" step summarising what the result should contain.
Known site names you may resolve to URLs: gmail, google, youtube, linkedin, amazon,
and the bundled demo site pages (demo mail, demo login, demo search, demo form,
demo table, demo download, demo home).
Return nothing except the {"steps": [...]} JSON object described above.
"""


@dataclass
class PlanMeta:
    source: str  # "llm" | "deterministic"
    reason: Optional[str] = None  # why LLM planning wasn't used / fell back, if applicable


def normalize_step(step: dict) -> dict:
    """Maps the alternative spellings an LLM may produce onto the executor's
    schema: navigate->goto, "url"->target, "text"->value. Pure and idempotent."""
    s = dict(step)
    action = s.get("action")
    s["action"] = _ACTION_ALIASES.get(action, action)
    if s["action"] == "goto" and not s.get("target") and s.get("url"):
        s["target"] = s["url"]
    if "text" in s and "value" not in s and s["action"] in ("type", "fill", "select", "press"):
        s["value"] = s["text"]
    if s["action"] == "press" and "value" not in s and s.get("key"):
        s["value"] = s["key"]
    return s


def _extract_site_url(text: str) -> Optional[str]:
    text_l = text.lower()
    for name in sorted(config.SITE_ALIASES, key=len, reverse=True):
        if name in text_l:
            return config.SITE_ALIASES[name]
    m = re.search(r"https?://[^\s]+", text)
    if m:
        return m.group(0)
    return None


_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "top": 0, "latest": 0, "newest": 0,
    "second": 1, "2nd": 1, "third": 2, "3rd": 2,
}


def _open_label(clause: str) -> str:
    text = clause.strip()
    if re.match(r"^(open|go to|navigate to|visit)\b", text, re.I):
        return text[0].upper() + text[1:]
    return f"Open {text}"


def _ordinal_from(text: str) -> Optional[int]:
    text_l = text.lower()
    for word, idx in _ORDINAL_WORDS.items():
        if word in text_l:
            return idx
    return None


def deterministic_plan(task: str) -> list[dict]:
    """Regex/keyword based planner — zero LLM calls, always available."""
    steps: list[dict] = []
    task_clean = task.strip().rstrip(".")

    # Handle "fill ... with field: value, field: value, ..." as one unit BEFORE
    # generic clause splitting, since that field list legitimately contains
    # commas that must not be treated as separate task clauses.
    fill_match = re.search(r"\bfill\b(.*?)\bwith\b(.+)$", task_clean, re.I)
    if fill_match:
        before, fields_part = fill_match.group(1), fill_match.group(2)
        url = _extract_site_url(before) or _extract_site_url(task_clean)
        if url:
            prefix = re.sub(r"(,|\band\b|\bthen\b|\s)+$", "", task_clean[:fill_match.start()], flags=re.I).strip()
            steps.append({"action": "goto", "target": url,
                          "label": _open_label(prefix or before.strip() or task_clean)})
        pairs = re.findall(r"([a-zA-Z ]+?)\s*[:=]\s*([^,]+)", fields_part)
        if pairs:
            for field, val in pairs:
                steps.append({
                    "action": "type", "target": field.strip() + " field", "value": val.strip(),
                    "label": f"Fill {field.strip()}",
                })
            steps.append({"action": "click", "target": "submit button", "label": "Submit the form"})
        else:
            # "fill the email field with test@example.com" -> one fill step
            field = re.sub(r"\b(the|field|box|input|in|form|out|this|a)\b", " ", before, flags=re.I).strip()
            value = fields_part.strip()
            if field and value:
                steps.append({
                    "action": "fill", "target": {"role": "textbox", "name": field}, "value": value,
                    "label": f"Fill {field}",
                })
        steps.append({"action": "finish", "target": None, "label": "Summarise the result"})
        return steps

    clauses = re.split(r",|;| and then | then | and ", task_clean)
    clauses = [c.strip() for c in clauses if c.strip()]
    last_site_hint = task_clean

    for clause in clauses:
        cl = clause.lower()

        # 1) navigation — only when the clause actually STARTS with a nav verb,
        # so a site name that happens to contain a word like "download" (e.g.
        # "demo download") doesn't get misread as a download action.
        if re.match(r"^(open|go to|navigate to|visit)\b", cl):
            url = _extract_site_url(clause)
            if url:
                steps.append({"action": "goto", "target": url, "label": _open_label(clause)})
                last_site_hint = clause
                continue

        # 1b) history navigation --------------------------------------------
        if re.match(r"^go back\b", cl) or cl == "back":
            steps.append({"action": "go_back", "target": None, "label": "Go back"})
            continue
        if re.match(r"^go forward\b", cl) or cl == "forward":
            steps.append({"action": "go_forward", "target": None, "label": "Go forward"})
            continue
        if re.match(r"^(press enter|hit enter)\b", cl):
            steps.append({"action": "press", "target": None, "value": "Enter", "label": "Press Enter"})
            continue
        wait_m = re.match(r"^wait(?:\s+(?:for\s+)?(\d+)\s*(?:s|sec|seconds)?)?\b", cl)
        if wait_m and "wait" == cl.split()[0]:
            secs = float(wait_m.group(1)) if wait_m.group(1) else 2.0
            steps.append({"action": "wait", "target": None, "value": secs, "label": f"Wait {secs:g}s"})
            continue

        # 1c) screenshot / scroll ---------------------------------------------
        if "screenshot" in cl:
            steps.append({"action": "screenshot", "target": None, "full_page": "full page" in cl,
                          "label": "Take a screenshot"})
            continue
        if re.match(r"^scroll\b", cl):
            direction = "top" if "top" in cl else "bottom" if "bottom" in cl else "up" if re.search(r"\bup\b", cl) else "down"
            step = {"action": "scroll", "target": None, "direction": direction, "label": f"Scroll {direction}"}
            amt = re.search(r"(\d+)", cl)
            if amt:
                step["amount"] = int(amt.group(1))
            steps.append(step)
            if "table" in cl:  # "scroll down to find the table"
                steps.append({"action": "extract_table", "target": "table", "label": "Extract the table"})
            continue

        # 2) search --------------------------------------------------------
        m = re.search(r"search(?:\s+(?:on|in))?\s*(.*?)\s*for\s+(.+)", cl)
        if "search" in cl and m:
            site_part, query = m.group(1), m.group(2)
            url = _extract_site_url(site_part) or _extract_site_url(last_site_hint)
            if url and not any(s.get("action") == "goto" and s.get("target") == url for s in steps):
                steps.append({"action": "goto", "target": url, "label": f"Open {site_part.strip() or last_site_hint}"})
            steps.append({
                "action": "type", "target": "search box", "value": query.strip(),
                "submit": True, "label": f'Search for "{query.strip()}"',
            })
            continue
        if cl.startswith("search for") or (cl.startswith("search") and "for" in cl):
            query = cl.split("for", 1)[1].strip()
            steps.append({
                "action": "type", "target": "search box", "value": query,
                "submit": True, "label": f'Search for "{query}"',
            })
            continue

        # 3) download --------------------------------------------------------
        if "download" in cl:
            steps.append({"action": "download", "target": "download link", "label": "Download the available file"})
            continue

        # 4) form filling -----------------------------------------------------
        if "fill" in cl and ("form" in cl or "with" in cl):
            with_part = cl.split("with", 1)[1] if "with" in cl else ""
            pairs = re.findall(r"([a-zA-Z ]+?)\s*[:=]\s*([^,]+)", with_part)
            if pairs:
                for field, val in pairs:
                    steps.append({
                        "action": "type", "target": field.strip() + " field", "value": val.strip(),
                        "label": f"Fill {field.strip()}",
                    })
                steps.append({"action": "click", "target": "submit button", "label": "Submit the form"})
            else:
                steps.append({"action": "goto", "target": _extract_site_url(cl) or "/demo/forms", "label": "Open form"})
            continue

        # 5) open nth / latest result / email --------------------------------
        if ("open" in cl or "click" in cl) and any(w in cl for w in ("result", "email", "message", "one", "link")):
            ordinal = _ordinal_from(cl) or 0
            steps.append({
                "action": "click", "target": clause.strip(), "ordinal": ordinal,
                "label": _open_label(clause),
            })
            continue

        # 5b) generic click on arbitrary hint text — "click the confirm
        # button", "click submit", "click the checkbox" — anything not
        # already covered by the ordinal result/email/link case above.
        # Without this, WebOrbit could only click a fixed vocabulary of
        # nouns, which contradicts the whole point of being general-purpose.
        click_m = re.match(r"^click(?:\s+(?:on|the))?\s+(.+)$", cl)
        if click_m:
            hint = click_m.group(1).strip()
            steps.append({"action": "click", "target": hint, "label": f"Click {hint}"})
            continue

        # 6) extract table -----------------------------------------------------
        if "table" in cl and ("extract" in cl or "read" in cl or "get" in cl or "find" in cl):
            steps.append({"action": "extract_table", "target": "table", "label": "Extract the table"})
            continue

        # 7) generic extraction / "tell me" / "find" -------------------------
        if any(w in cl for w in ("tell me", "extract", "find the", "what is", "who is")):
            hint = re.sub(r"^(tell me|extract|find the|what is|who is)\s*", "", cl).strip() or "page content"
            steps.append({"action": "extract_text", "target": hint, "label": f"Read: {hint}"})
            continue

        # 8) fallback: treat the whole clause as a navigation target ---------
        url = _extract_site_url(clause)
        if url:
            steps.append({"action": "goto", "target": url, "label": _open_label(clause)})

    if not steps:
        # nothing matched at all — just try to open whatever site was named
        url = _extract_site_url(task_clean) or "/demo/"
        steps.append({"action": "goto", "target": url, "label": f"Open {task_clean}"})

    steps.append({"action": "finish", "target": None, "label": "Summarise the result"})
    return steps


class LLMUnavailable(Exception):
    """Raised with a human-readable, classified reason so the caller can
    log a truthful message instead of silently swallowing the failure."""


async def _call_openrouter(messages: list[dict]) -> str:
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                config.OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": config.OPENROUTER_MODEL, "temperature": 0.1, "messages": messages},
            )
    except httpx.TimeoutException:
        raise LLMUnavailable("OpenRouter request timed out")
    except httpx.ConnectError:
        raise LLMUnavailable("could not reach OpenRouter (connection error)")
    except Exception as e:
        raise LLMUnavailable(f"OpenRouter request failed: {e}")

    if resp.status_code == 429:
        raise LLMUnavailable("OpenRouter rate-limited this request (429)")
    if resp.status_code == 401:
        raise LLMUnavailable("OpenRouter rejected the API key (401)")
    if resp.status_code >= 500:
        raise LLMUnavailable(f"OpenRouter is temporarily unavailable ({resp.status_code})")
    if resp.status_code != 200:
        raise LLMUnavailable(f"OpenRouter returned HTTP {resp.status_code}")

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        raise LLMUnavailable("OpenRouter response had an unexpected shape")


def _strip_code_fences(content: str) -> str:
    """Pulls the content out of a ```json ... ``` (or bare ``` ... ```)
    fence, wherever it appears in the response — even with prose before
    or after it. Returns the original text, trimmed, if there is no fence."""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    return m.group(1).strip() if m else content.strip()


def _find_balanced(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    """Returns the first balanced open_ch..close_ch substring in `text`,
    tracking JSON string literals so a bracket inside a quoted string
    (e.g. a URL or label) can't end the match early. Used as a fallback
    when the response isn't pure JSON (stray prose around it, etc.)."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_steps_list(parsed) -> Optional[list]:
    """Pulls the list of steps out of already-parsed JSON, whether the
    model returned a bare array or an object wrapping it, e.g.
    {"steps": [...]} (preferred, per the system prompt) or the equally
    reasonable {"plan": [...]} / {"actions": [...]}."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("steps", "plan", "actions"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
    return None


def _parse_steps(content: str) -> list[dict]:
    text = _strip_code_fences(content)

    # 1) the common, well-behaved case: the (defenced) text is itself
    #    valid JSON — either a bare array or {"steps": [...]}.
    steps = None
    try:
        steps = _extract_steps_list(json.loads(text))
    except Exception:
        pass

    # 2) direct parse failed (stray prose around otherwise-valid JSON,
    #    a trailing sentence, etc.) — fall back to extracting the first
    #    balanced block. Try an object first ({"steps": [...]} is what the
    #    prompt now asks for), then a bare array, since some models will
    #    still return one regardless of what's requested.
    if steps is None:
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            block = _find_balanced(text, open_ch, close_ch)
            if not block:
                continue
            try:
                candidate = _extract_steps_list(json.loads(block))
            except Exception:
                continue
            if candidate is not None:
                steps = candidate
                break

    if steps is None:
        raise LLMUnavailable('could not find a JSON step array or {"steps": [...]} object in the model\'s response')
    if not isinstance(steps, list):
        raise LLMUnavailable('model response\'s "steps" field was not a JSON array')

    cleaned = [normalize_step(s) for s in steps
               if isinstance(s, dict) and (s.get("action") in ACTIONS or s.get("action") in _ACTION_ALIASES)]
    cleaned = [s for s in cleaned if s.get("action") in ACTIONS]
    if not cleaned:
        raise LLMUnavailable("model response contained no valid steps")
    if cleaned[-1].get("action") != "finish":
        cleaned.append({"action": "finish", "target": None, "label": "Summarise the result"})
    return cleaned


async def llm_plan(task: str) -> list[dict]:
    """Calls OpenRouter to plan the task. If the first response can't be
    parsed into steps, this makes exactly ONE controlled retry with a
    stricter, example-bearing instruction before giving up — never more
    than one, so a persistently uncooperative model can't turn into a
    retry loop. Raises LLMUnavailable with a classified, human-readable
    reason if both attempts fail — callers should catch this and fall
    back to the deterministic planner, logging the real reason."""
    if not config.OPENROUTER_API_KEY:
        raise LLMUnavailable("no OPENROUTER_API_KEY configured")

    content = await _call_openrouter([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ])
    try:
        return _parse_steps(content)
    except LLMUnavailable as e:
        # Python clears the `as e` binding once the except block exits, so
        # it must be captured into a plain variable to use below.
        first_error = e

    retry_prompt = (
        f"Your previous response could not be parsed ({first_error}).\n"
        "Return ONLY valid JSON — no Markdown, no ```json fences, no explanation, "
        "no text before or after the JSON. Return exactly one JSON object of the shape "
        '{"steps": [...]}, where every item has a valid "action" field from the list in '
        f"the system prompt.\n\nTask: {task}"
    )
    try:
        retry_content = await _call_openrouter([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": retry_prompt},
        ])
    except LLMUnavailable:
        raise first_error  # the retry call itself failed (e.g. rate limit) — surface the original parse reason
    return _parse_steps(retry_content)  # bounded: if this also fails, it raises and propagates — no further retry


async def replan_on_failure(task: str, dom_summary: str, failure_reason: str) -> Optional[list[dict]]:
    """Sparingly used: only called when a step's target could not be
    resolved deterministically. Asks the LLM (if configured) for ONE
    replacement step given real, current DOM state. Returns None (rather
    than raising) on any failure — re-planning is a best-effort extra, the
    caller already has a sensible fallback (ask the user)."""
    if not config.OPENROUTER_API_KEY:
        return None
    prompt = (
        f"Original task: {task}\nA step failed: {failure_reason}\n\n"
        f"Current page state:\n{dom_summary}\n\n"
        "Given the real elements listed above, output a JSON array with ONE replacement "
        "step (same schema as before) that best continues the task, or an empty array [] "
        "if the task cannot proceed."
    )
    try:
        content = await _call_openrouter([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        steps = _parse_steps(content)
        return [s for s in steps if s.get("action") != "finish"] or None
    except LLMUnavailable:
        return None


async def plan_task(task: str) -> tuple[list[dict], PlanMeta]:
    """Returns (steps, meta). meta.source tells the caller which planner
    actually produced the plan, and meta.reason explains why, if OpenRouter
    was configured but a real failure sent it to the deterministic fallback
    (project spec section 20: never crash, never fake a response, always
    surface the real reason)."""
    if config.OPENROUTER_API_KEY:
        try:
            steps = await llm_plan(task)
            return steps, PlanMeta(source="llm")
        except LLMUnavailable as e:
            return deterministic_plan(task), PlanMeta(source="deterministic", reason=str(e))
    return deterministic_plan(task), PlanMeta(source="deterministic", reason="no API key configured")
