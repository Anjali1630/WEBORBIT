"""
WebOrbit backend.

One process, one port:
  GET  /                -> dashboard (frontend/index.html) — UNCHANGED from v1
  GET  /api/config       -> LLM mode + status
  GET  /api/history       -> task history list
  GET  /api/history/{id}  -> one task's full record, including its execution trace
  GET  /api/metrics        -> real, computed evaluation metrics from history
  WS   /ws               -> the live agent session
  *    /demo/*            -> the bundled local demo site (see demo_site/main.py)

v2 notes: this file keeps the exact same WebSocket message *shapes* the v1
frontend expects (config/status/log/steps/frame/await_login/login_resolved/
await_confirm_domain/result/history_update) — the UI was a hard "do not
change" requirement, so all the new functionality (state machine, retries,
re-planning, verification, execution trace, capability routing) is plumbed
through those same message types rather than new ones.
"""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urljoin

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import config, history, metrics
from .agent.executor import BrowserSession, DomainNotAllowed
from .agent import capabilities, guardrail
from .agent.dom_inspector import summarize_for_llm
from .llm import planner
from demo_site.main import demo

app = FastAPI(title="WebOrbit")
app.mount("/demo", demo)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

MAX_ATTEMPTS = 3  # bounded retry policy (project spec section 15): retry, re-inspect+retry, replan, then stop


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/config")
async def api_config():
    return {"llm_mode": config.LLM_MODE, "model": config.OPENROUTER_MODEL if config.OPENROUTER_API_KEY else None}


@app.get("/api/history")
async def api_history():
    return JSONResponse(history.list_entries())


@app.get("/api/history/{entry_id}")
async def api_history_detail(entry_id: str):
    entry = history.get_entry(entry_id)
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(entry)


@app.get("/api/metrics")
async def api_metrics():
    return JSONResponse(metrics.compute_metrics())


# ===========================================================================
# Agent session (one per WebSocket connection)
# ===========================================================================
class AgentSession:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.browser: BrowserSession | None = None
        self._screencast_task: asyncio.Task | None = None
        self._task_runner: asyncio.Task | None = None
        self._running = False
        self._cancel = False
        self._login_wait_event = asyncio.Event()
        self._pending_domain_event = asyncio.Event()
        self._pending_domain_ok = False
        self._base = f"http://{config.HOST}:{config.PORT}"

    async def send(self, payload: dict):
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def log(self, message: str):
        await self.send({"type": "log", "time": time.strftime("%H:%M:%S"), "message": message})

    async def state(self, name: str):
        """Broadcasts an explicit agent-state-machine transition. The v1 UI
        just uppercases and displays whatever string lands in the existing
        status pill, so every state below (OBSERVING, VALIDATING, EXECUTING,
        VERIFYING, REPLANNING, WAITING_FOR_USER, ...) renders correctly with
        zero frontend changes."""
        await self.send({"type": "status", "state": name})

    async def ensure_browser(self):
        if self.browser is None:
            self.browser = BrowserSession()
            await self.browser.start()
            self._start_screencast()

    def _start_screencast(self):
        async def _loop():
            while True:
                try:
                    if self.browser and self.browser.page:
                        frame = await self.browser.screenshot_b64()
                        await self.send({"type": "frame", "image": frame, "url": self.browser.current_url()})
                except Exception:
                    pass
                await asyncio.sleep(1.0 / config.SCREENCAST_FPS)
        self._screencast_task = asyncio.create_task(_loop())

    async def shutdown(self):
        """Deterministic, ordered teardown — fixes a real race condition
        that existed in v2: closing the browser *before* the screencast
        loop had actually stopped could hit the browser mid-screenshot,
        and the run_task coroutine (spawned fire-and-forget on every
        "run_task" message) was never tracked or cancelled at all, so a
        disconnect mid-task left it running against a browser that was
        being torn down out from under it. Both are exactly the kind of
        thing that manifests as an intermittent hang or an orphaned
        Chromium process, so the order here matters:

          1. cancel + AWAIT the task-runner (the in-flight run_task, if any)
          2. cancel + AWAIT the screencast loop
          3. only then close the browser/context/Playwright

        Each cancellation is awaited to completion (not just requested)
        before moving to the next step, so nothing is still touching the
        browser when we finally close it.
        """
        for task in (self._task_runner, self._screencast_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        if self.browser:
            await self.browser.close()
            self.browser = None

    def _resolve_url(self, target: str) -> str:
        """Resolve LLM navigation targets safely.

        The planner may return a configured site alias such as "demo-form",
        "demo form", "youtube", or "google" instead of a full URL. Resolve
        those aliases through config.SITE_ALIASES before the domain guardrail
        sees the target. Unknown bare targets are returned unchanged so the
        existing allow-list/confirmation guardrail still handles them.
        """
        target = (target or "").strip()

        if not target:
            return target

        if target.startswith("http://") or target.startswith("https://"):
            return target

        # Normalize harmless formatting differences produced by an LLM:
        # demo-form == demo_form == demo form
        normalized = " ".join(
            target.lower().replace("-", " ").replace("_", " ").split()
        )

        for alias, url in config.SITE_ALIASES.items():
            alias_normalized = " ".join(
                alias.lower().replace("-", " ").replace("_", " ").split()
            )
            if normalized == alias_normalized:
                if url.startswith("/"):
                    return urljoin(self._base, url)
                return url

        if target.startswith("/"):
            return urljoin(self._base, target)

        return target

    async def handle_client_message(self, data: dict):
        mtype = data.get("type")
        if mtype == "run_task":
            if not self._running:
                self._task_runner = asyncio.create_task(self.run_task(data.get("task", "").strip()))
        elif mtype == "cancel":
            self._cancel = True
        elif mtype == "login_done":
            self._login_wait_event.set()
        elif mtype == "confirm_domain":
            self._pending_domain_ok = bool(data.get("allow"))
            self._pending_domain_event.set()
        elif mtype == "mouse_click" and self.browser and self.browser.page:
            await self.browser.mouse_click_relative(data.get("x", 0), data.get("y", 0))
        elif mtype == "key_type" and self.browser and self.browser.page:
            await self.browser.keyboard_type(data.get("text", ""))
        elif mtype == "key_press" and self.browser and self.browser.page:
            await self.browser.keyboard_press(data.get("key", "Enter"))

    # ---- login handoff (unchanged behaviour from v1, still generic) --------

    async def _await_manual_login(self):
        await self.state("waiting_for_user")
        await self.log("Authentication required")
        await self.send({"type": "await_login"})
        self._login_wait_event.clear()
        await self.log("Waiting for user login")
        try:
            await asyncio.wait_for(self._wait_for_login_resolved(), timeout=config.LOGIN_WAIT_TIMEOUT_S)
            await self.log("Authentication detected")
            await self.send({"type": "login_resolved"})
        except asyncio.TimeoutError:
            await self.log("Timed out waiting for login")

    async def _wait_for_login_resolved(self):
        while True:
            if self._login_wait_event.is_set():
                return
            if self.browser and not self.browser.is_login_url():
                try:
                    dom = await self.browser.inspect()
                    has_pw = any(e["type"] == "password" and e["visible"] for e in dom["elements"])
                except Exception:
                    has_pw = False
                if not has_pw:
                    return
            await asyncio.sleep(1.0)

    async def _confirm_domain(self, domain: str) -> bool:
        await self.state("waiting_for_user")
        await self.log(f"'{domain}' is not on the allow-list — asking for confirmation")
        self._pending_domain_event.clear()
        await self.send({"type": "await_confirm_domain", "domain": domain})
        try:
            await asyncio.wait_for(self._pending_domain_event.wait(), timeout=config.CONFIRM_DOMAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False
        return self._pending_domain_ok

    # ---- single-attempt action dispatch ------------------------------------
    # Returns (ok, tool, validation_status, error) where:
    #   tool             "playwright" | "http_api" | "none"
    #   validation_status "ok" | "rejected" | "n/a"

    async def _run_action_once(self, step: dict, result_summary: dict) -> tuple[bool, str, str, str | None]:
        action = step.get("action")

        if action == "goto":
            url = self._resolve_url(step["target"])
            await self.state("navigating")
            await self.log(f"Navigating to {url}")
            try:
                await self.browser.goto(url)
            except DomainNotAllowed as e:
                allowed = await self._confirm_domain(e.domain)
                if not allowed:
                    await self.log(f"User declined to visit {e.domain} — skipping")
                    return False, "playwright", "n/a", "domain declined by user"
                await self.browser.goto(url, force=True)
            await self.log("Page loaded")
            if self.browser.is_login_url():
                await self._await_manual_login()
            else:
                await self.state("observing")
                dom = await self.browser.inspect()
                if any(e["type"] == "password" and e["visible"] for e in dom["elements"]):
                    await self._await_manual_login()
            return True, "playwright", "n/a", None

        if action == "type":
            hint, value = step.get("target", "text field"), step.get("value", step.get("text", ""))
            await self.state("observing")
            await self.log(f"Inspecting DOM for '{guardrail.target_text(hint)}'")
            fp_before = await self.browser.fingerprint()
            await self.state("executing")
            target = await self.browser.type_hint(hint, value, submit=bool(step.get("submit")))
            if not target.ok:
                await self.log(f"Could not resolve target: {target.reason}")
                return False, "playwright", "rejected", target.reason
            await self.log(f"Target validated ({target.reason})")
            await self.log(f"Entered: {value}")
            if step.get("submit"):
                await self.log("Submitted")
                await self.state("verifying")
                fp_after = await self.browser.fingerprint()
                if fp_after["url"] == fp_before["url"] and fp_after["text_hash"] == fp_before["text_hash"]:
                    await self.log("Verification warning: page did not appear to change after submitting")
                else:
                    await self.log("Verified: page updated after submitting")
            return True, "playwright", "ok", None

        if action == "click":
            hint, ordinal = step.get("target", "element"), step.get("ordinal")
            await self.state("observing")
            await self.log(f"Inspecting DOM for '{guardrail.target_text(hint)}'")
            await self.state("executing")
            target = await self.browser.click_hint(hint, ordinal=ordinal)
            if not target.ok:
                await self.log(f"Could not resolve target: {target.reason}")
                return False, "playwright", "rejected", target.reason
            await self.log(f"Target validated ({target.reason})")
            await self.log("Clicked")
            if self.browser.is_login_url():
                await self._await_manual_login()
            return True, "playwright", "ok", None

        if action == "select":
            hint, value = step.get("target", "dropdown"), step.get("value", "")
            await self.state("executing")
            target = await self.browser.select_hint(hint, value)
            if not target.ok:
                await self.log(f"Could not resolve target: {target.reason}")
                return False, "playwright", "rejected", target.reason
            await self.log(f"Selected '{value}' ({target.reason})")
            return True, "playwright", "ok", None

        if action == "fill":
            hint, value = step.get("target"), step.get("value", step.get("text", ""))
            await self.state("observing")
            await self.log(f"Inspecting DOM for '{guardrail.target_text(hint)}'")
            await self.state("executing")
            target = await self.browser.fill_hint(hint, value)
            if not target.ok:
                await self.log(f"Could not resolve target: {target.reason}")
                return False, "playwright", "rejected", target.reason
            await self.log(f"Target validated ({target.reason})")
            await self.log(f"Filled: {value}")
            return True, "playwright", "ok", None

        if action == "scroll":
            direction = step.get("direction") or "down"
            await self.state("executing")
            try:
                before, after = await self.browser.scroll(direction, step.get("amount") or 600)
            except ValueError as e:
                await self.log(f"Scroll rejected: {e}")
                return False, "playwright", "n/a", str(e)
            if after == before:
                await self.log(f"Scrolled {direction} (page position unchanged at y={after} — top/bottom reached or page not scrollable)")
            else:
                await self.log(f"Scrolled {direction} (y: {before} -> {after})")
            return True, "playwright", "n/a", None

        if action == "screenshot":
            await self.state("executing")
            await self.log("Taking screenshot...")
            ok, detail = await self.browser.screenshot(step.get("path"), bool(step.get("full_page")))
            if not ok:
                await self.log(f"Screenshot failed: {detail}")
                return False, "playwright", "n/a", detail
            result_summary["screenshot"] = detail
            await self.log(f"Screenshot saved: {Path(detail).name}")
            return True, "playwright", "ok", None

        if action == "press":
            key = step.get("value") or "Enter"
            await self.state("executing")
            await self.browser.press_key(key)
            await self.log(f"Pressed {key}")
            return True, "playwright", "n/a", None

        if action == "wait":
            seconds = float(step.get("value") or 2.0)
            await self.log(f"Waiting {seconds:g}s")
            await self.browser.wait_for(seconds)
            return True, "playwright", "n/a", None

        if action in ("back", "go_back"):
            await self.state("executing")
            await self.browser.go_back()
            await self.log("Went back")
            return True, "playwright", "n/a", None

        if action in ("forward", "go_forward"):
            await self.state("executing")
            await self.browser.go_forward()
            await self.log("Went forward")
            return True, "playwright", "n/a", None

        if action == "extract_text":
            hint = step.get("target", "page content")
            await self.state("executing")
            target, text = await self.browser.extract_text_hint(hint)
            result_summary["text"] = text
            await self.log(f"Extracted text for '{hint}'")
            return True, "playwright", "ok" if target.ok else "n/a", None

        if action == "extract_table":
            # API-first: try a plain static fetch+parse before spending a
            # full Playwright DOM walk. Generic — no site-specific knowledge.
            await self.state("observing")
            url = self.browser.current_url()
            static_rows = await capabilities.try_static_table_fetch(url)
            if static_rows:
                await self.log("Used a lightweight HTTP fetch (API-first accelerator) for the table")
                result_summary["table"] = static_rows
                await self.state("verifying")
                await self.log(f"Verified: {len(static_rows)} row(s) extracted")
                return True, "http_api", "ok", None
            await self.state("executing")
            rows = await self.browser.extract_table()
            result_summary["table"] = rows
            await self.log(f"Extracted table via Playwright ({len(rows)} rows)")
            await self.state("verifying")
            if not rows:
                await self.log("Verification failed: no table rows found on the page")
                return False, "playwright", "n/a", "extracted table was empty"
            await self.log(f"Verified: {len(rows)} row(s) extracted")
            return True, "playwright", "ok", None

        if action == "download":
            hint = step.get("target", "download link")
            await self.state("executing")
            target, path = await self.browser.download_hint(hint)
            if not path:
                await self.log(f"Download failed: {target.reason}")
                return False, "playwright", "rejected" if target.woid is None else "n/a", target.reason
            result_summary["download"] = path
            await self.log(f"Downloaded file to {Path(path).name}")
            await self.state("verifying")
            ok, detail = BrowserSession.verify_download(path)
            if not ok:
                await self.log(f"Verification failed: {detail}")
                return False, "playwright", "n/a", detail
            await self.log(f"Verified download: {detail}")
            return True, "playwright", "ok", None

        if action == "finish":
            await self.log("Task complete")
            return True, "none", "n/a", None

        return False, "none", "n/a", f"unknown action '{action}'"

    # ---- bounded retry / re-plan wrapper ------------------------------------

    async def _execute_step(self, task: str, step: dict, result_summary: dict, trace_entry: dict) -> bool:
        """Implements project-spec section 15's bounded retry policy:
        attempt 1 = try it; attempt 2 = re-inspect + retry the same intent;
        attempt 3 = ask the LLM to re-plan against the real current DOM;
        beyond that we stop rather than loop forever, and surface it clearly
        (WAITING_FOR_USER) instead of silently failing."""
        current_step = dict(step)
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            trace_entry["retry_count"] = attempt - 1
            t0 = time.monotonic()
            try:
                ok, tool, validation_status, error = await self._run_action_once(current_step, result_summary)
            except Exception as e:
                ok, tool, validation_status, error = False, "playwright", "n/a", str(e)
            trace_entry["tool"] = tool
            trace_entry["validation_status"] = validation_status
            trace_entry["duration_ms"] = round((time.monotonic() - t0) * 1000)
            trace_entry["error"] = error
            if validation_status == "rejected":
                # the guardrail stopped this attempt BEFORE anything was executed
                trace_entry["blocked_count"] = trace_entry.get("blocked_count", 0) + 1

            if ok:
                return True

            last_error = error
            if self._cancel:
                return False

            # only "rejected" (guardrail couldn't resolve a target) is worth
            # retrying/re-planning — a domain decline or a genuinely unknown
            # action isn't going to change on a second attempt.
            if validation_status != "rejected":
                return False
            if (error or "").startswith("no target specified"):
                break  # a plan step with no target can never resolve; retrying is pointless

            if attempt == 1:
                await self.log("Retrying the same step")
                # brief settle window — covers pages whose content (e.g. a
                # button's label/enabled state) finishes changing shortly
                # after the initial load, without inventing a fake DOM wait.
                await asyncio.sleep(1.6)
                continue

            if attempt == 2:
                await self.state("replanning")
                await self.log("Re-inspecting page")
                dom = await self.browser.inspect()
                dom_summary = summarize_for_llm(dom)
                replacement = await planner.replan_on_failure(task, dom_summary, last_error or "target not found")
                if replacement:
                    await self.log("Re-planned step using current page state")
                    trace_entry["replan_count"] = 1
                    current_step = replacement[0]
                    continue
                await self.log("No safe re-plan available")
                break

        await self.state("waiting_for_user")
        await self.log(f"Needs your input — couldn't resolve '{step.get('target')}' after {MAX_ATTEMPTS} attempts. "
                        f"You can complete this step manually in the live browser.")
        return False

    # ---- main task loop ------------------------------------------------------

    async def run_task(self, task: str):
        if not task:
            return
        self._running = True
        self._cancel = False
        entry = history.create_entry(task)
        await self.state("planning")
        await self.send({"type": "status", "state": "planning", "task": task})
        await self.log(f'New task: "{task}"')

        await self.log("Understanding task")
        steps, plan_meta = await planner.plan_task(task)
        steps = [planner.normalize_step(st) for st in steps]
        if plan_meta.source == "deterministic" and plan_meta.reason and plan_meta.reason != "no API key configured":
            await self.log(f"Planning note: {plan_meta.reason} — used the deterministic planner instead")
        await self.log(f"Plan ready — {len(steps)} step(s) ({plan_meta.source})")
        history.update_entry(entry["id"], plan_source=plan_meta.source, plan_reason=plan_meta.reason,
                             mode=config.mode_label(plan_meta.source))

        step_states = [{"label": s.get("label") or s["action"], "status": "pending"} for s in steps]
        history.update_entry(entry["id"], steps=step_states)
        await self.send({"type": "steps", "steps": step_states})

        await self.ensure_browser()
        result_summary = {"text": None, "table": None, "download": None, "screenshot": None}
        trace: list[dict] = []
        final_status = "success"

        for i, step in enumerate(steps):
            if self._cancel:
                final_status = "cancelled"
                break
            step_states[i]["status"] = "active"
            await self.send({"type": "steps", "steps": step_states})

            trace_entry = {
                "action": step.get("action"), "target": step.get("target"),
                "tool": None, "validation_status": None, "duration_ms": None,
                "error": None, "retry_count": 0, "replan_count": 0, "blocked_count": 0, "status": None,
            }
            try:
                ok = await self._execute_step(task, step, result_summary, trace_entry)
            except Exception as e:
                ok = False
                trace_entry["error"] = str(e)
                await self.log(f"Step error: {e}")
            trace_entry["status"] = "done" if ok else "failed"
            trace.append(trace_entry)
            step_states[i]["status"] = "done" if ok else "failed"
            await self.send({"type": "steps", "steps": step_states})
            history.update_entry(entry["id"], steps=step_states, trace=trace)

            if not ok:
                # By the time _execute_step returns False, retry + re-inspect
                # + re-plan have already been exhausted — this step is not
                # recoverable. Every later step (including "finish") implicitly
                # depends on this one having succeeded, so continuing would
                # let a trailing "finish" step log "Task complete" and mark
                # itself done for a task that did not, in fact, complete.
                # Stop here: remaining steps stay "pending" (never attempted,
                # which is the truth), not silently skipped-but-green.
                final_status = "failed"
                break

        await self.state("completed" if final_status == "success" else "failed")
        history.finish_entry(entry["id"], final_status, result=result_summary)
        await self.send({"type": "result", "status": final_status, "result": result_summary})
        await self.send({"type": "status", "state": "idle"})
        await self.send({"type": "history_update", "history": history.list_entries()[:20]})
        self._running = False


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = AgentSession(ws)
    await session.send({"type": "config", "llm_mode": config.LLM_MODE})
    await session.send({"type": "history_update", "history": history.list_entries()[:20]})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            await session.handle_client_message(data)
    except WebSocketDisconnect:
        pass
    finally:
        await session.shutdown()
