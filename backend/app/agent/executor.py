"""
BrowserSession — a thin, careful wrapper around one real Playwright page.

Every method here is a *tool*: a small, named, validated capability. Nothing
in this file accepts arbitrary code from the LLM. The planner can only ever
select one of these tools and supply plain data (a URL, a hint, some text) —
never a script, a shell command, or a raw selector it invented.
"""
import asyncio
import base64
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from .. import config
from .dom_inspector import INSPECT_JS
from . import guardrail


class DomainNotAllowed(Exception):
    def __init__(self, domain: str):
        self.domain = domain
        super().__init__(f"domain not on allow-list: {domain}")


def domain_allowed(url: str) -> bool:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in config.ALLOWED_DOMAIN_SUFFIXES)


class BrowserSession:
    def __init__(self):
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        self.context = await self.browser.new_context(viewport=config.VIEWPORT, accept_downloads=True)
        self.page = await self.context.new_page()

    async def close(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ---- navigation -----------------------------------------------------

    async def goto(self, url: str, force: bool = False):
        if not force and not domain_allowed(url):
            raise DomainNotAllowed(urlparse(url).netloc)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await self._settle()

    async def _settle(self):
        try:
            await self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass

    def current_url(self) -> str:
        return self.page.url if self.page else ""

    def is_login_url(self) -> bool:
        url = self.current_url().lower()
        return any(hint in url for hint in config.LOGIN_URL_HINTS)

    # ---- DOM inspection ---------------------------------------------------

    async def inspect(self) -> dict:
        return await self.page.evaluate(INSPECT_JS)

    # ---- validated tools --------------------------------------------------

    async def click_hint(self, hint: str, ordinal: Optional[int] = None) -> guardrail.ResolvedTarget:
        dom = await self.inspect()
        target = guardrail.resolve(dom["elements"], hint, ordinal=ordinal)
        if target.ok:
            await self.page.click(f'[data-woid="{target.woid}"]', timeout=6000)
            await self._settle()
        return target

    async def type_hint(self, hint: str, text: str, submit: bool = False) -> guardrail.ResolvedTarget:
        dom = await self.inspect()
        target = guardrail.resolve(dom["elements"], hint)
        if target.ok:
            locator = self.page.locator(f'[data-woid="{target.woid}"]')
            await locator.click(timeout=6000)
            await locator.fill("", timeout=4000)
            await locator.type(text, delay=15)
            if submit:
                await self.page.keyboard.press("Enter")
                await self._settle()
        return target

    async def fill_hint(self, hint, text: str) -> guardrail.ResolvedTarget:
        """Playwright fill(): sets the field value directly (no keystrokes, no
        submit), then reads it back to confirm it actually took effect."""
        dom = await self.inspect()
        target = guardrail.resolve(dom["elements"], hint)
        if target.ok:
            locator = self.page.locator(f'[data-woid="{target.woid}"]')
            await locator.fill(text, timeout=4000)
            try:
                actual = await locator.input_value(timeout=2000)
            except Exception:
                actual = text  # contenteditable etc. — no value to read back
            if actual != text:
                raise RuntimeError(f"fill did not take effect (field contains {actual!r})")
        return target

    async def scroll(self, direction: str = "down", amount: int = 600) -> tuple[int, int]:
        """Real page scroll via window.scrollBy/scrollTo. Returns (y_before, y_after)."""
        direction = (direction or "down").lower()
        if direction not in ("down", "up", "top", "bottom"):
            raise ValueError(f"unsupported scroll direction '{direction}'")
        amount = max(1, min(int(amount or 600), 10000))
        before = await self.page.evaluate("window.scrollY")
        if direction == "top":
            await self.page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            await self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        else:
            dy = amount if direction == "down" else -amount
            await self.page.evaluate(f"window.scrollBy(0, {dy})")
        await asyncio.sleep(0.15)
        after = await self.page.evaluate("window.scrollY")
        return int(before), int(after)

    async def screenshot(self, filename: Optional[str] = None, full_page: bool = False) -> tuple[bool, str]:
        """Real Playwright page.screenshot() into config.SCREENSHOT_DIR.
        `filename` must be a plain file name (no directories / traversal).
        Returns (ok, path_or_error). The file is verified to exist and be
        non-empty; a failed/empty capture is removed."""
        import time as _time
        if filename is None:
            filename = f"screenshot_{int(_time.time() * 1000)}.png"
        if (not filename or Path(filename).name != filename or filename.startswith(".")
                or Path(filename).suffix.lower() not in (".png", ".jpg", ".jpeg")):
            return False, f"invalid screenshot file name '{filename}' (use a plain name ending in .png/.jpg)"
        dest = config.SCREENSHOT_DIR / filename
        try:
            await self.page.screenshot(path=str(dest), full_page=full_page)
            if not dest.exists() or dest.stat().st_size == 0:
                raise RuntimeError("screenshot file is missing or empty")
            return True, str(dest)
        except Exception as e:
            try:
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink()
            except Exception:
                pass
            return False, f"screenshot failed: {e}"

    async def press_enter(self):
        await self.page.keyboard.press("Enter")
        await self._settle()

    async def press_key(self, key: str):
        await self.page.keyboard.press(key)
        await self._settle()

    async def select_hint(self, hint: str, value: str) -> guardrail.ResolvedTarget:
        dom = await self.inspect()
        candidates = [e for e in dom["elements"] if e["tag"] == "select"]
        target = guardrail.resolve(candidates or dom["elements"], hint)
        if target.ok:
            locator = self.page.locator(f'[data-woid="{target.woid}"]')
            try:
                await locator.select_option(label=value, timeout=4000)
            except Exception:
                await locator.select_option(value=value, timeout=4000)
            await self._settle()
        return target

    async def wait_for(self, seconds: float = 1.0):
        await asyncio.sleep(max(0.0, min(seconds, 10.0)))
        await self._settle()

    async def go_back(self):
        await self.page.go_back(timeout=15000)
        await self._settle()

    async def go_forward(self):
        await self.page.go_forward(timeout=15000)
        await self._settle()

    # ---- verification -------------------------------------------------------

    async def fingerprint(self) -> dict:
        """A cheap snapshot of page state, used to VERIFY an action actually
        did something rather than assuming success just because a click or
        type call didn't raise."""
        try:
            body_text = await self.page.inner_text("body")
        except Exception:
            body_text = ""
        return {"url": self.current_url(), "text_len": len(body_text), "text_hash": hash(body_text[:5000])}

    @staticmethod
    def verify_download(path: Optional[str]) -> tuple[bool, str]:
        """Confirms a download didn't just fire an event, but actually
        produced a real, non-empty file on disk."""
        if not path:
            return False, "no file path recorded"
        p = Path(path)
        if not p.exists():
            return False, f"file does not exist: {path}"
        size = p.stat().st_size
        if size == 0:
            return False, f"file is empty: {path}"
        return True, f"{size} bytes"

    async def extract_text_hint(self, hint: str) -> tuple[guardrail.ResolvedTarget, str]:
        dom = await self.inspect()
        target = guardrail.resolve(dom["elements"], hint)
        text = ""
        if target.ok:
            locator = self.page.locator(f'[data-woid="{target.woid}"]')
            text = (await locator.inner_text())[:2000]
        else:
            # fall back to page body text if nothing specific matched
            text = (await self.page.inner_text("body"))[:2000]
            target = guardrail.ResolvedTarget(None, "fell back to full page text", [], True)
        return target, text

    async def extract_table(self) -> list[list[str]]:
        # findDeep pierces open shadow roots (same reasoning as
        # dom_inspector.py's queryAllDeep) so a table inside a web
        # component isn't invisible to extraction either.
        rows = await self.page.evaluate(
            """() => {
                function findDeep(root, selector) {
                    const hit = root.querySelector(selector);
                    if (hit) return hit;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const found = findDeep(el.shadowRoot, selector);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                const table = findDeep(document, 'table');
                if (!table) return [];
                return Array.from(table.querySelectorAll('tr')).slice(0, 50).map(
                    tr => Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim())
                );
            }"""
        )
        return rows

    async def download_hint(self, hint: str = "download") -> tuple[guardrail.ResolvedTarget, Optional[str]]:
        dom = await self.inspect()
        target = guardrail.resolve(dom["elements"], hint)
        if not target.ok:
            return target, None
        try:
            async with self.page.expect_download(timeout=10000) as dl_info:
                await self.page.click(f'[data-woid="{target.woid}"]', timeout=6000)
            download = await dl_info.value
            dest = config.DOWNLOAD_DIR / download.suggested_filename
            await download.save_as(str(dest))
            return target, str(dest)
        except Exception as e:
            return guardrail.ResolvedTarget(target.woid, f"download failed: {e}", [], False), None

    # ---- raw manual control (used only while WAITING FOR USER / login) ---

    async def mouse_click_relative(self, rel_x: float, rel_y: float):
        x = rel_x * config.VIEWPORT["width"]
        y = rel_y * config.VIEWPORT["height"]
        await self.page.mouse.click(x, y)

    async def keyboard_type(self, text: str):
        await self.page.keyboard.type(text, delay=15)

    async def keyboard_press(self, key: str):
        await self.page.keyboard.press(key)

    # ---- live view ---------------------------------------------------------

    async def screenshot_b64(self) -> str:
        img = await self.page.screenshot(type="jpeg", quality=55)
        return base64.b64encode(img).decode("ascii")
