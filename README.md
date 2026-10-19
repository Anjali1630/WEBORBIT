# WebOrbit — AI Browser Control Center

Give WebOrbit a plain-English task and it drives a real Chromium browser
(via Playwright) to get it done — navigating, searching, filling forms,
extracting data, downloading files — while you watch it work in a live
browser view, with a full step-by-step plan and activity log alongside it.
If a site asks for a login, WebOrbit pauses, lets you take over the
controls right inside the same live view, and continues once you're
signed in.

It is **general-purpose**: there is no `if site == "gmail"` anywhere in
this codebase. The agent understands a task, opens the real page,
inspects the actual DOM to see what's there right now, and decides what
to click/type/read from that — the same code path whether the page is
Gmail, Amazon, LinkedIn, or a page it has never seen before.

## Quick start

**Requirements:** Python 3.10+ and a Chromium binary (installed via
`python -m playwright install chromium` below). **Node.js is not required** —
`frontend/index.html` is a single static file with no build step, no
npm packages, and no bundler; the FastAPI backend serves it directly.

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -r requirements.txt
python -m playwright install chromium                             # downloads the browser binary
cp .env.example .env                                    # optional: add an OpenRouter key
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser. That's the whole app —
one Python process serves the dashboard, the WebSocket agent, and a
bundled local demo site (`/demo/...`) for reliable testing.

No `OPENROUTER_API_KEY`? WebOrbit still fully works — it runs in
**LOCAL DEMO FALLBACK** mode, using a deterministic keyword/regex planner
instead of an LLM. Add a free-tier OpenRouter key to `.env` to switch to
**OPENROUTER FREE** planning (`OPENROUTER_MODEL=openrouter/free` by default).

## Try it

Type a task in the command bar, or use a quick action. A few that work
out of the box against the bundled demo site (no internet needed):

- "Open the demo mail app and search for internship emails"
- "Open the demo search page and search for python courses"
- "Fill the demo form with name: Anjali, email: anjali@example.com, message: hello"
- "Open the demo table page and extract the table"
- "Open the demo download page and download the report"

And against the real internet (needs your machine's own network access):

- "Open Gmail and search for emails from my college" — pauses for you to log in
- "Open YouTube and search for C-drama recommendations"
- "Open LinkedIn and search for data analyst jobs"

## How it works

```
TASK
 │
 ▼
TASK UNDERSTANDING & PLAN   ← ONE LLM call (or the deterministic parser
 │                              if no API key is set) — never per-click
 ▼
CAPABILITY CHECK  ──► API available for this step? ──► use it, validate, done
 │  (no)
 ▼
PLAYWRIGHT (the universal fallback)
 │
 ▼
DOM INSPECTION  → tags every visible interactive element on the real page
 │
 ▼
GUARDRAIL  → target must EXIST, be VISIBLE, be ENABLED, be UNIQUE
 │  (fails)                              │ (passes)
 ▼                                        ▼
RE-INSPECT → RE-TARGET → RE-PLAN        EXECUTE → OBSERVE → next step
 (asks the LLM again ONLY here,
  with real current-page context)
```

Key files:

| File | Responsibility |
|---|---|
| `backend/app/llm/planner.py` | One-shot task planning: deterministic parser + optional OpenRouter call, with classified error handling (rate limit/timeout/bad response) |
| `backend/app/agent/dom_inspector.py` | JS injected into the live page to enumerate real interactive elements — role, accessible name, associated `<label>`, test id, plus page structure (headings/forms/tables) |
| `backend/app/agent/guardrail.py` | Resolves a plain-language hint ("search box") against the real DOM using a priority-ordered targeting strategy (testid > accessible name > label > name/id > text); enforces exists/visible/enabled/unique |
| `backend/app/agent/executor.py` | The only code that touches Playwright — validated tools (goto, click, type, select, press, wait, go_back, go_forward, extract, download) + result verification helpers |
| `backend/app/agent/capabilities.py` | The "API available?" accelerator tier — a generic static HTTP fetch+parse tried before Playwright for read-only table extraction |
| `backend/app/main.py` | WebSocket orchestrator: explicit agent state machine, bounded retry/re-plan, execution trace, live screenshot feed, login hand-off + domain confirmation |
| `backend/app/metrics.py` | Computes real evaluation metrics (success rates, retry/re-plan rates, capability routing) from actual task history — nothing invented |
| `backend/demo_site/main.py` | The bundled `/demo/*` sandbox (search, mail, forms, table, download, login) — for reliable testing only; the agent has no special-case knowledge of it |
| `backend/tests/` | Real automated tests — unit (planner, guardrail), HTTP integration, and full end-to-end tests against a live server + real Playwright browser |
| `frontend/index.html` | The dashboard — single-file HTML/CSS/JS, unchanged since v1, talks to `/ws` |

## Agent state machine

Every task run broadcasts explicit state transitions over the existing
status channel: `IDLE → PLANNING → NAVIGATING → OBSERVING → EXECUTING →
VERIFYING → (REPLANNING) → (WAITING_FOR_USER) → COMPLETED/FAILED → IDLE`.
The dashboard's status pill just displays whichever string arrives, so you
can watch the agent's real internal state as it works without any UI changes.

## Bounded retry & re-planning

Per action: **attempt 1** tries the resolved target; if the guardrail
couldn't find it, **attempt 2** re-inspects the live DOM and retries the
same intent (pages can finish loading late); if it still can't resolve,
**attempt 3** asks the LLM to re-plan against the real, current page state
(only if an OpenRouter key is configured — otherwise this step is skipped
safely). If all three attempts fail, WebOrbit stops retrying, reports
`WAITING_FOR_USER`, and clearly logs that you can finish the step yourself
in the live browser — it never loops forever and never guesses a click.

## Result verification

WebOrbit doesn't call an action successful just because it didn't throw:

- **download** — confirms the file actually exists on disk and is non-empty
- **extract_table** — confirms at least one row came back; an empty table fails the step
- **type + submit** (search-like actions) — snapshots the page before and
  after, and logs whether the page actually changed

## API-first capability routing

For read-only table extraction, WebOrbit first tries a plain, generic
static HTTP fetch + HTML parse (`agent/capabilities.py`) — no site-specific
code, works against any URL. If that comes back empty (dynamic content, a
login-gated page, non-HTML response), it transparently falls back to the
already-open, already-authenticated Playwright page. Which path was used
is recorded per-action (`tool: "http_api"` vs `"playwright"`) and shown in
`/api/metrics`.

## Execution trace & metrics

Every action now records a structured trace entry — action, target, tool
used, validation status, duration, error, retry count, re-plan count —
stored per task in `data/history.json` and viewable at
`GET /api/history/{id}`. `GET /api/metrics` aggregates real numbers across
every run so far: task/action success rates, validation rejection rate,
retry rate, re-plan rate, and API-vs-Playwright routing. If no tasks have
run yet, rates report `null` rather than a misleading `0%`.

## Human-in-the-loop login

When WebOrbit lands on anything that looks like a login page (URL pattern
or a visible password field), it stops and shows **"Waiting for you to
log in"**. You can click directly on the live browser image (your click
is translated into a real `page.mouse.click` at that position) and type
into the box under it (forwarded as real keystrokes into the page) — so
you're genuinely typing your own credentials into the real page, not into
a fake overlay. Once you're signed in, either the page's own URL change is
detected automatically, or you can click **"I've finished logging in"**.

## Security & guardrails

- The LLM never runs code, a shell command, or a raw selector — it can
  only select one of a small set of named tools and supply plain data.
- Every action is validated against the *real* DOM immediately before
  it runs (`agent/guardrail.py`). Ambiguous or missing targets trigger a
  re-inspect / re-plan instead of a blind click.
- Navigating to a domain outside the built-in allow-list
  (`app/config.py: ALLOWED_DOMAIN_SUFFIXES`) pauses and asks you to
  confirm first.
- Credentials are never touched by the agent — logins are always
  completed by you, live, in the visible page.

## Running the tests

```bash
cd backend
pip install -r requirements.txt   # includes pytest, pytest-asyncio, websockets
python -m playwright install chromium
python3 -m pytest tests/ -v
```

This runs 41 real tests: unit tests for the planner and guardrail (no
server needed), HTTP integration tests against the real FastAPI app, and
end-to-end tests that start a real uvicorn server and drive a real headless
Chromium through the demo site over the actual WebSocket protocol —
navigation, form fill, search+verify, download+verify, the bounded
retry/re-plan path, the full state-machine transition sequence, and the
domain-confirmation gate.

## Notes

- Screenshots are streamed at ~2.5 fps (`WEBORBIT_FPS` in `.env`) rather
  than a full video/VNC stream, to keep things dependency-light. It's
  enough to see navigation, typing, and results happening in near
  real time.
- Task history is stored in `data/history.json` (flat file — fine for a
  single-user demo; swap in a real DB for multi-user use).
- The deterministic planner (`llm/planner.py: deterministic_plan`)
  understands the phrasing patterns in the project brief (open/search/
  fill/download/extract/ordinal picks/multi-step "X, then Y, then Z").
  It's intentionally simple regex/keyword matching — the DOM guardrail is
  what makes execution safe and general, not clever language understanding.

---

## v6 changelog (shadow DOM + fail-fast step loop)

**UI unchanged.** Touched: `app/agent/dom_inspector.py`, `app/agent/executor.py`
(extract_table only), `app/config.py` (one new demo alias), `app/main.py`
(step-loop control flow only), `demo_site/main.py` (new `/demo/shadow`
page), plus tests. No rollback was needed — nothing in v2–v5 had regressed;
both bugs below predated every prior patch and were simply never exercised
by a page that used shadow DOM or by a plan where an early step failed.

### Bug 1 — diagnosed, not assumed: DOM inspection doesn't pierce shadow DOM

With a real OpenRouter key, `Open YouTube and search for C-drama
recommendations` opened YouTube but failed at "Inspecting DOM for 'search
box'" — found nothing, even though the search box was visibly on screen.
I could not load real youtube.com from this sandbox (its egress proxy
returns a hard 403 for that host) to verify directly, so rather than
guess, I built `/demo/shadow` — a page with a real custom element two
shadow roots deep, modeling how YouTube's own search box is encapsulated —
and confirmed empirically: the inspector's `document.querySelectorAll`
found 0 elements inside it before the fix, and the real `<input>`/
`<button>`/`<table>` after. Fix: a recursive `queryAllDeep()` added to
both places that enumerate elements (the inspector's JS, and
`extract_table`'s JS) that descends into every *open* shadow root it
finds. Closed shadow roots remain genuinely unreachable — there's no way
around that from page-side JS, and the code says so rather than silently
pretending otherwise. No change to guardrail scoring, the planner, or the
executor's click/type/fill logic — those already worked correctly once
the element could be found at all, since Playwright's own `page.locator()`
already pierces shadow DOM natively.

### Bug 2 — real, separate issue: a failed step didn't stop the task

Reported pattern: `Search ❌`, `Summarise result ✓`, `Task complete` — a
task whose critical step failed was still reported as having completed.
Root cause, confirmed by reading the loop directly: `main.py`'s step loop
only `break`s on cancellation, never on a step failing after retries are
exhausted — so a trailing `finish` step (and any other step after the
failure) still ran, and `finish` unconditionally logs "Task complete" and
marks itself done. Fix: `break` immediately once a step fails and recovery
is exhausted. Steps after the failure now correctly stay `pending` (never
attempted) instead of silently running and reporting green.

### Tests: 83 → 88 (all run, all passing)

```
test_demo_shadow_page_defines_the_web_components ............... PASSED
test_dom_inspector_pierces_shadow_dom ........................... PASSED
test_search_inside_shadow_dom_succeeds_end_to_end ............... PASSED
test_extract_table_inside_shadow_dom ............................ PASSED
test_failed_step_stops_the_task_instead_of_continuing ........... PASSED
```
Full suite, run three times consecutively from a clean state: **88
passed, 0 failed, 0 errors**, ~30–33s each run, 0 orphaned Chromium/uvicorn
processes after any run.

### Manual verification (live server, no API key — matches the reported scenario)

1. `Open the demo home` — plain navigation, analogous to "Open YouTube" alone: succeeds.
2. `Open the demo shadow page and search for C-drama recommendations` —
   the literal reported failure, reproduced against a real nested-shadow-DOM
   page instead of a mock: `Inspecting DOM for 'search box'` → `Target
   validated (matched 'search box' via accessible_name)` → `Entered: ...`
   → `Submitted` → `Verified: page updated after submitting` → success.
3. `Open the demo table page, click the missing purchase widget and
   extract the table` — a genuinely unresolvable target: fails cleanly
   after 3 bounded attempts, and both later steps stay `pending` —
   "Task complete" is never logged.

### What I could and couldn't verify

**Verified by actual execution:** both bugs, reproduced against real
(if local) pages exhibiting the same structural issue, fixed, and covered
by regression tests. **Not verified:** the literal real youtube.com page —
this sandbox cannot reach it (confirmed 403, not assumed). I'm confident
in the shadow-DOM diagnosis because I built and tested against a page with
the same *structural* property (a search box genuinely encapsulated in a
shadow root), not because I assumed YouTube uses shadow DOM without
checking anything. If YouTube's actual masthead turns out to use a
different encapsulation mechanism, please re-test on your machine and
report back what the live activity log shows now.

---

## v5 changelog (OpenRouter response-parsing robustness)

**UI unchanged.** Only `backend/app/llm/planner.py` and its tests were touched.

### The bug

With a real OpenRouter key connected (confirmed: `LLM: OPENROUTER FREE`),
`Open YouTube and search for C-drama recommendations` fell back to the
deterministic planner with: *"could not parse a JSON step array out of the
model's response."* The old parser (`re.search(r"\[.*\]", content,
re.DOTALL)`) only reliably handled a bare JSON array with nothing else
around it. A real model response wrapped in markdown fences, wrapped in an
object (`{"steps": [...]}`), or with any prose/stray brackets before or
after the JSON could make that greedy regex grab the wrong span (e.g. from
a bracket in incidental prose text all the way to an unrelated closing
bracket much later), producing something `json.loads` can't parse — even
though the actual plan JSON was perfectly valid.

### The fix

`_parse_steps` now, in order: (1) strips a ` ```json ... ``` ` fence
wherever it appears in the response, prose before/after included; (2)
tries a direct `json.loads` on what's left, accepting either a bare array
or `{"steps": [...]}` (also `{"plan": [...]}` / `{"actions": [...]}`); (3)
only if that fails, falls back to a proper **balanced**-bracket extractor
(tracks nesting and JSON string literals, so a bracket inside a quoted
URL or label can't end the match early) — tried as an object first, then
an array. `llm_plan()` also now makes exactly **one** controlled retry
with a stricter, example-bearing instruction if the first response can't
be parsed, before giving up — bounded, not a loop; verified by a test that
asserts exactly 2 calls happen, never more, whether the retry succeeds or
not. The system prompt was tightened to explicitly request
`{"steps": [...]}` with no Markdown/fences/prose, while the parser still
accepts a bare array too, since not every model will comply exactly.

Existing action names, structured `{"role", "name"}` targets, and all
alias normalization (`navigate`→`goto`, `extract`→`extract_text`, `url`/
`text`/`key` field mapping) are unchanged and still pass through untouched.

### Tests: 77 → 83 (all new ones actually run)

Added to `test_llm_error_handling.py`, each matching one of the six
required formats:
```
test_object_with_steps_key_is_parsed .......................... PASSED
test_markdown_fenced_json_is_parsed ............................ PASSED
test_bare_array_in_markdown_fence_is_parsed .................... PASSED
test_structured_target_survives_object_with_steps_parsing ...... PASSED
test_one_controlled_retry_on_malformed_response_then_succeeds .. PASSED
test_retry_is_bounded_to_exactly_one_attempt ................... PASSED
```
(Malformed JSON and invalid-action rejection were already covered by
existing tests, which still pass unchanged.)

Full suite, run twice consecutively from a clean state:
**83 passed, 0 failed, 0 errors**, ~29–34s each run, 0 orphaned processes
after either run.

### What I could and couldn't verify

**Verified by actual execution:** the exact parsing failure (object-wrapped
and markdown-fenced responses, including one shaped like a real
`{"role":"textbox","name":"Search"}` fill step for the YouTube task) is
reproduced against a mocked transport and confirmed fixed, with the full
suite passing. **Not verified:** a live call to the real OpenRouter API —
no key or network access to openrouter.ai exists in this environment, same
limitation as every prior pass. I can't confirm this is the *only* way a
real `openrouter/free` response could fail to parse, only that the
specific, reported failure mode is fixed and guarded by a regression test.

---

## v4 changelog (gap-closing pass — minimal, targeted patch)

**UI: still byte-for-byte unchanged** — md5 verified against the original
v1 file before and after. This pass touched only `app/config.py`,
`app/history.py`, `app/metrics.py`, `app/agent/guardrail.py`,
`app/agent/executor.py`, `app/llm/planner.py`, `app/main.py`,
`demo_site/main.py`, and added `tests/test_metrics.py` plus new tests in
the existing test files. No existing test was weakened, skipped, or removed.

### What was added (each gap closed individually)

1. **`screenshot` action** — real `page.screenshot()` into
   `data/screenshots/`, verified non-empty on disk, rejected on an invalid
   file name, cleaned up on a failed/empty capture.
2. **`scroll` action** — real `window.scrollBy`/`scrollTo`, with
   direction (`down`/`up`/`top`/`bottom`) and pixel amount; logs the
   actual before/after scroll position.
3. **`fill` action** — Playwright `.fill()` (sets the value directly,
   no keystrokes/submit), reads the value back to confirm it took effect.
   `type` is unchanged and still does the keystroke+optional-submit behavior.
4. **`back`/`forward` as canonical action names** — `go_back`/`go_forward`
   still work unchanged (`if action in ("back", "go_back")`), both accepted.
5. **Structured targets** — `guardrail.resolve()` now accepts either a
   plain string (unchanged path, unchanged behavior) or
   `{"role": ..., "name": ...}` (role is a hard filter: the wrong role is
   never substituted for the right one — it's blocked and falls through to
   the existing retry/re-plan logic, never a misclick).
6. **`mode` in task history** — `"OpenRouter Free"` / `"Local Demo
   Fallback"`, computed from the existing `plan_source` via
   `config.mode_label()`. `plan_source` itself is unchanged and still present.
7. **`blocked_action_count` in `/api/metrics`** — increments only when the
   guardrail rejected a target before anything executed, counted separately
   from `actions.failed` (a failed download-verification or an empty table
   is a real failure but not a "block").
8. **Planner alias normalization** — `normalize_step()` maps `navigate`→
   `goto` and a `text`/`url` field onto `value`/`target`, so a slightly
   different but reasonable shape from the LLM doesn't get silently dropped.
9. **README** — states plainly that Node.js is not required and why.

### Tests: 53 → 77 (all new tests actually run, all passing)

| File | Before | After |
|---|---|---|
| `test_planner.py` | 12 | 18 |
| `test_guardrail.py` | 10 | 16 |
| `test_llm_error_handling.py` | 8 | 8 (unchanged) |
| `test_http_integration.py` | 13 | 14 |
| `test_metrics.py` | — | 4 (new file) |
| `test_agent_e2e.py` | 10 | 17 |
| **Total** | **53** | **77** |

Run individually (exact output from this session):
```
test_planner.py ............... 18 passed
test_guardrail.py ............. 16 passed
test_llm_error_handling.py .... 8 passed
test_http_integration.py ...... 14 passed
test_metrics.py ............... 4 passed
test_agent_e2e.py ............. 17 passed in 28.5s
```
Full suite, run twice consecutively from a clean state: **77 passed, 0
failed, 0 errors**, ~29s each time, 0 orphaned processes after either run.

### Manual tests performed (live server, no API key)

1. `Open the demo home and take a screenshot of this page` — real PNG
   written to `data/screenshots/`, confirmed with `file` (1280×800 PNG).
2. `Open the demo long page, scroll down 500 pixels to find the table` —
   log confirmed `Scrolled down (y: 0 -> 500)`, then extracted all 60 rows.
3. `Open the demo form page and fill the email field with test@example.com`
   — confirmed via log and by the guardrail's own "Filled: ..." line.
4. `Open the demo table page, open the demo search page and then go back`
   — confirmed "Went back" in the log.
5. `Open the demo search page and click the search button` — confirmed the
   generic click resolved and clicked via accessible-name matching.
6. Checked `/api/history` afterward: every entry's `mode` field read
   `"Local Demo Fallback"`, `plan_source` still `"deterministic"`.
7. Checked `/api/metrics`: real numbers from the accumulated session
   (`blocked_action_count: 20` from earlier deliberately-unresolvable-target
   tests in this and prior sessions, `api_accelerator_used: 13`, etc.) —
   nothing fabricated.

**OpenRouter Free mode:** no API key is available in this environment (and
this sandbox can't reach openrouter.ai regardless), so live network testing
still could not be performed — same limitation as the prior two passes,
stated plainly rather than faked. The structured-target schema, alias
normalization, and error classification are unit-tested against a mocked
transport in `test_llm_error_handling.py`, unchanged from before.

---



**UI: still byte-for-byte unchanged** — verified again by md5sum before and
after this pass. This pass found and fixed real bugs; it did not touch
`frontend/index.html`.

### The real bug behind the reported test hang

`ws_endpoint`'s cleanup had two related lifecycle problems, both in
`app/main.py`:

1. **Wrong teardown order.** The `finally` block closed the Playwright
   browser *before* cancelling the screencast loop, and never awaited the
   cancellation. If the screencast task was mid-`screenshot()` when the
   browser closed underneath it, that call could hang or throw
   unpredictably, and cancellation requested-but-not-awaited meant the
   event loop had no guarantee the task had actually stopped before the
   connection handler returned.
2. **Untracked background task.** Every `"run_task"` message did
   `asyncio.create_task(self.run_task(...))` with the return value
   discarded. If the client disconnected mid-task, that coroutine kept
   running completely unsupervised — still calling into a browser that the
   `finally` block was simultaneously tearing down.

**Fix:** `AgentSession` now tracks both tasks and has one `shutdown()`
method with a strict, awaited order: (1) cancel + await the task-runner,
(2) cancel + await the screencast loop, (3) only then close the browser.
Nothing is still touching the browser when it closes.

**How this was verified**, not just reasoned about: a standalone script
opens a WebSocket, starts a task, waits until the agent reports the
`navigating` state (so the browser is genuinely up and mid-navigation),
then closes the connection *abruptly* — no clean finish, no "idle"
message. Ran this 6 times in a row. Before the fix this is exactly the
kind of race that produces an intermittent hang or an orphaned Chromium
process; after the fix, `ps aux | grep chromium` showed **zero** orphaned
processes across all 6 runs, and the server remained healthy and
responsive to new connections immediately after each one.

I want to be precise about what I could and couldn't reproduce: I was not
able to make the full suite actually hang in this environment, in many
repeated runs (including the literal `cd backend && python -m pytest -q`
from the report). The fix above targets a genuine, demonstrable race
condition that matches every symptom in the report (screencast loop,
asyncio tasks, unclosed browser/context, background tasks) and is now
covered by a repeatable stress test — but I can't claim certainty it was
*the* cause on the original machine, only that it was a real bug, it's
fixed, and it's now guarded against regressing.

As a secondary safety net (not the fix, a hardening): the login/domain
-confirm wait timeouts (180s/60s in normal use) are now configurable via
env vars, and the test fixture pins them to 20s/10s — so if any *test*
ever accidentally triggers one of those waits, it fails fast in seconds
instead of blocking for minutes in a way indistinguishable from a hang.

### Other real bugs found and fixed while adding coverage

- **Deterministic planner couldn't click arbitrary elements.** The "click"
  handling only recognized a fixed noun list (result/email/message/link) —
  asking it to "click the confirm button" silently produced no click step
  at all. Generalized to a "click the/on X" pattern for any hint text.
  This was a real generalization gap (item 9 in the brief), not just a
  test convenience — it directly contradicts the "not restricted to a
  fixed vocabulary" requirement.
- **My own login-handoff test was flaky by construction** (fixed before
  it shipped, not after): the first version guessed pixel coordinates for
  the login form. Replaced with an "oracle browser" technique — a
  throwaway Playwright page loads the exact same static page at the exact
  same viewport size and reads the *real* element positions via
  `bounding_box()`, then those exact coordinates drive the agent's browser
  through the same mouse/keyboard WebSocket messages a human would use.
  Deterministic, not guessed; passes in ~5s.
- **A self-inflicted infinite recursion in the new OpenRouter mock tests**
  (`test_llm_error_handling.py`): monkeypatching `httpx.AsyncClient` to a
  factory function that itself called `httpx.AsyncClient(...)` recursed
  into itself. Fixed by capturing the real class before patching it —
  this was a bug in the test, not the app, caught immediately by running
  the new tests rather than assuming they'd pass.
- **A wrong assertion in my own new HTTP test**: checking that "confirm
  button" doesn't appear anywhere in `/demo/delayed`'s HTML failed because
  that exact string legitimately appears inside the page's inline
  `<script>` source (the JS that sets it as an aria-label at runtime).
  Fixed to check for the literal HTML *attribute* form instead.

### New tests added (all run, all passing)

The suite grew from 41 to **53** tests:

| New test | Project-spec item | What it proves |
|---|---|---|
| `test_login_handoff_and_resume` | 7 | Real `WAITING FOR USER` → real login via mouse/keyboard forwarding → real `LOGIN DETECTED` → task resumes and completes |
| `test_retry_recovers_from_late_rendering_element` | 6 | A page whose button starts disabled/unlabeled and changes ~1.3s later is *successfully* recovered by the bounded retry (not just failed cleanly, which was the only case v2 tested) |
| `test_generic_click_on_arbitrary_hint` | 9 | The click-generalization fix above |
| `test_demo_delayed_page_starts_disabled` | — | The new `/demo/delayed` fixture page itself |
| `tests/test_llm_error_handling.py` (8 tests) | 4 | Valid plan parsing, 429 rate-limit classification, 5xx classification, malformed JSON rejection, unknown-action filtering, timeout handling, and deterministic fallback — all via a mocked transport, no real key needed |

### What was tested this pass (and results)

- `python -m compileall -q app tests demo_site` from `backend/` — **clean, exit 0**
- `python -m pytest -q` from `backend/` — **53 passed**, run **3 times consecutively** with zero failures and zero orphaned processes after each
- `GET /`, `/api/config`, `/api/history`, `/api/metrics` — all verified live against a running server started with `OPENROUTER_API_KEY` explicitly unset: confirmed `"llm_mode": "LOCAL DEMO FALLBACK"`, and all four endpoints returned real, correctly-shaped data (including a full history with real execution traces and metrics computed from 85 accumulated dev-session runs)
- All 7 no-key demo tasks from item 3 — actually run against the live server, all completed (one, "fill the demo table page with an unrelated field", is *supposed* to fail cleanly and does — that's the guardrail working, not a bug)
- OpenRouter mode (item 4): **no real API key was available in this environment**, and this sandbox's network egress doesn't reach `openrouter.ai` regardless — so a live network round-trip genuinely could not be tested here. What *was* tested, thoroughly, via a mocked HTTP transport (no real key needed): structured-action parsing, rejection of malformed/unrecognized output, and non-crashing classified handling of 429/5xx/timeout responses. If you have a real key, `cp .env.example .env`, fill it in, and the existing `/api/config` output will show `"OPENROUTER FREE"` — that live path is unchanged from v2 and ready to test on your machine.
- Guardrail (item 6): existing/missing/disabled/ambiguous element cases were already covered in v2's unit tests; this pass added the genuine *recovery* case (changed content resolved via retry) that was missing before
- Resource lifecycle (item 10): verified via the abrupt-disconnect stress test described above — 6 runs, 0 orphaned Chromium processes each time, server stays healthy

### Known limitations (unchanged from what's structurally true, restated honestly)

- OpenRouter's live behavior (real rate limits, real model output quality) is
  untested here for the reasons above — the code path is exercised end-to-end
  via mocks, but a real network round-trip needs your own key and network access.
- The hang I fixed is a genuine, verified bug, but since I couldn't reproduce
  the original hang here, I can't certify it was the *only* possible cause on
  a different machine. If a hang recurs, the first things to check are: (a)
  is anything else already bound to port 8123 during test runs (the
  session-scoped `live_server` fixture uses a fixed port), and (b) does that
  machine's Chromium need `--no-sandbox`/`--disable-dev-shm-usage` (common in
  restrictive Docker/CI setups) — WebOrbit's current launch args don't set
  these because they weren't needed in this sandbox; add them to
  `agent/executor.py: BrowserSession.start()` if your environment requires them.
- `WAITING_FOR_USER` after exhausted retries is still a log message + failed
  step, not a blocking modal, per the same "don't touch the UI" constraint as v2.

### Exact commands to run it

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                 # optional: add an OpenRouter key
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000

# validation, in order:
python3 -m compileall -q app tests demo_site
python3 -m pytest -q
```

---

## v2 changelog (functionality-only pass)

**UI: byte-for-byte unchanged.** `frontend/index.html`'s md5 hash is
identical before and after this pass — verified, not just claimed. Every
new capability below is plumbed through the *existing* WebSocket message
types (`status`, `log`, `steps`, `result`), so the dashboard shows richer
information (real state-machine states, verification results, re-plan
attempts) without a single line of frontend code changing.

### What changed

- **DOM inspection** — now computes accessible name, associated `<label>`,
  test id, implicit ARIA role, and page structure (headings/forms/tables
  count), not just placeholder/name/text.
- **Targeting strategy** — `guardrail.py` rewritten around an explicit
  priority order (test id → accessible name → label → name/id → text →
  role), instead of one flat keyword score.
- **New tools** — `select`, `press`, `wait`, `go_back`, `go_forward` added
  alongside the existing goto/click/type/extract/download.
- **Bounded retry + re-plan** — replaces v1's single-shot fallback with the
  spec's 3-attempt policy (retry → re-inspect+retry → LLM re-plan), then a
  clean `WAITING_FOR_USER` stop instead of silent failure or infinite retry.
- **Result verification** — downloads are checked on disk (exists,
  non-empty), table extraction rejects empty results, and search/submit
  actions are checked for an actual page change.
- **Explicit agent state machine** — IDLE/PLANNING/NAVIGATING/OBSERVING/
  EXECUTING/VERIFYING/REPLANNING/WAITING_FOR_USER/COMPLETED/FAILED, all
  broadcast over the existing status channel.
- **API-first capability routing** — a generic static-HTML accelerator for
  table extraction, with automatic Playwright fallback; which path was
  used is recorded per action.
- **Classified LLM error handling** — OpenRouter rate limits, timeouts,
  auth errors, and malformed responses are now distinguished and logged
  with the real reason, instead of a silent catch-all fallback.
- **Execution trace + `/api/metrics`** — every action's tool, validation
  status, duration, retries, and re-plans are recorded; metrics are
  computed from real history, never invented.
- **Structured plan schema** — the OpenRouter system prompt and the
  deterministic planner now share one explicit action/field schema.

### What was tested (and how)

All 41 tests below were actually executed in this session, not just
written — see the `python3 -m pytest tests/ -v` output: **41 passed**.

| Suite | What it exercises | Result |
|---|---|---|
| `tests/test_guardrail.py` (10) | Priority-ordered targeting, ambiguity rejection, visibility/enabled gating, ordinal picks | ✅ all pass |
| `tests/test_planner.py` (11) | Every phrasing pattern in the brief (open/search/fill/download/extract/ordinal/multi-step/go back/press enter), OpenRouter-absent fallback | ✅ all pass |
| `tests/test_http_integration.py` (12) | Real FastAPI routes, demo site pages, login cookie flow, 404 handling | ✅ all pass |
| `tests/test_agent_e2e.py` (8) | Real Playwright browser against a real live server: navigate+extract via the API accelerator, form fill, search+verify, download+verify, bounded retry/re-plan on a genuinely unresolvable target, full state-machine transition sequence, domain-confirmation gate, real metrics | ✅ all pass |

Also manually re-verified after every change (via a raw WebSocket test
client, logged in full during development): table extraction, form fill,
search, download, the login hand-off with live mouse/keyboard forwarding,
and the unfamiliar-domain confirmation gate — all matching v1 behavior
with the new verification/trace logging layered on top.

One real bug was **found and fixed** by the test suite itself: the
targeting-strategy tokenizer didn't split on hyphens/underscores, so a
`data-testid="search-input"` wasn't recognized as matching a "search box"
hint. Fixed in `guardrail._tokens()`; the regression test
(`test_testid_beats_everything_else`) now guards it.

### Known limitations

- The deterministic planner is regex/keyword based, as specified — it
  handles the phrasings in the brief well but isn't full language
  understanding. Real natural-language robustness comes from configuring
  an OpenRouter key.
- Re-planning only fires for "target not found" failures (validation
  rejections). A domain decline or a truly unknown action isn't retried,
  since re-running it wouldn't change the outcome.
- `WAITING_FOR_USER` after exhausted retries is a log message + failed
  step, not a blocking modal — there was no existing UI element to block
  on without violating the "don't change the UI" requirement, and you can
  always finish the step by hand in the live browser view.
- The API-first accelerator only covers `extract_table` (the one action
  where a static fetch can be meaningfully and generically compared
  against a full Playwright DOM walk); other actions always use Playwright.
- This container's own network egress is restricted to package registries,
  so live tests against real internet sites (Gmail/YouTube/LinkedIn) were
  only verified for graceful error handling here, not full success — they
  worked in earlier v1 testing when network access was available and
  should work the same way on your machine.
- Screenshot streaming stays at ~2.5 fps, not full video — a deliberate
  dependency-light choice carried over from v1, not a regression.

### Exact commands to run it

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                 # optional: add an OpenRouter key
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000

# to run the tests:
python3 -m pytest tests/ -v
```
