"""
Capability router — the "API available?" half of the architecture in the
project brief:

    TASK -> CAPABILITY CHECK -> API available? --yes--> API, validate, result
                                       |
                                       no
                                       v
                                  PLAYWRIGHT (DOM inspect, validate, execute)

This is intentionally generic: it is a plain static-HTML fetch + parse used
ONLY as a faster accelerator for read-only extraction (extract_table today),
tried against whatever URL the task is currently on. It carries no
knowledge of any specific website. If the page needs JavaScript, a login
session, or anything dynamic, the static fetch will simply come back empty
or mismatched and the caller falls back to the already-open, already
-authenticated Playwright page — which is always the universal fallback.
"""
import re
from html.parser import HTMLParser
from typing import Optional

import httpx


class _TableParser(HTMLParser):
    """Minimal, dependency-free <table> extractor — good enough to avoid
    needing a full browser for plain, static, server-rendered tables."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None
        self._table: Optional[list[list[str]]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._table = []
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._in_table:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            if self._table:
                self.tables.append(self._table)
            self._in_table = False
            self._table = None
        elif tag == "tr" and self._in_table and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            if self._row is not None:
                self._row.append(text)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


async def try_static_table_fetch(url: str, timeout: float = 4.0) -> Optional[list[list[str]]]:
    """The 'API' tier for extract_table: a plain GET + parse, no browser.
    Returns the first non-trivial table found, or None if the static fetch
    didn't yield anything usable (dynamic page, needs auth, network error,
    non-HTML response, etc.) — in which case the caller should fall back
    to Playwright's live, already-authenticated page."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "WebOrbit/1.0 (+capability-check)"})
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type:
            return None
        parser = _TableParser()
        parser.feed(resp.text)
        for table in parser.tables:
            rows = [r for r in table if any(cell.strip() for cell in r)]
            if len(rows) >= 2:  # header + at least one data row
                return rows
        return None
    except Exception:
        return None
