"""A headless Firefox that stays logged in to chat.google.com and proxies API calls.

The browser is the session keeper: Google rotates cookies via RotateCookies while the
page is open, and the persistent profile keeps them across runs. API calls are made
with fetch() *inside* the page, so they carry exactly the cookies/origin the real web
client uses. We borrow the XSRF token and RequestHeader the page itself sends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Request, async_playwright

from .cookies import load_google_cookies

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "gchat-tui"
PROFILE_DIR = DATA_DIR / "profile"
HOME_URL = "https://chat.google.com/app/home"
API = "https://chat.google.com/u/0/api/"


class AuthError(RuntimeError):
    pass


class Session:
    def __init__(self, headless: bool = True, cookie_browser: str = "auto"):
        self.headless = headless
        self.cookie_browser = cookie_browser  # where to import a Google login from
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.xsrf: str | None = None
        # RequestHeader exactly as the web client sends it (field 100 of most requests)
        self.request_header: list | None = None
        self._ready = asyncio.Event()

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self.ctx = await self._pw.firefox.launch_persistent_context(
                str(PROFILE_DIR), headless=self.headless, viewport={"width": 1400, "height": 900}
            )
        except Exception as e:
            if "already running" in str(e):
                raise RuntimeError(
                    "gchat-tui is already running (the browser profile is in use). Quit the other one first."
                ) from None
            raise
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.page.on("request", self._sniff)
        if not await self._load():
            log.info("Not logged in; importing cookies (%s)", self.cookie_browser)
            await self._import_cookies()
            if not await self._load():
                raise AuthError(
                    "Could not log in to Google Chat. Sign in to chat.google.com in your "
                    "browser (or pick one with --browser), then restart gchat-tui."
                )
        try:
            await asyncio.wait_for(self._ready.wait(), 30)
        except TimeoutError as e:
            raise AuthError("Logged in, but never saw the page make an API call") from e

    async def _load(self) -> bool:
        self._ready.clear()
        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        return self.page.url.startswith("https://chat.google.com/")

    async def recover(self) -> None:
        """Reload the page; if Google logged us out, re-import cookies from Firefox."""
        self.request_header = None
        if not await self._load():
            await self._import_cookies()
            if not await self._load():
                raise AuthError("Logged out, and re-importing browser cookies didn't work")
        await asyncio.wait_for(self._ready.wait(), 30)

    async def _import_cookies(self) -> None:
        try:
            cookies = load_google_cookies(self.cookie_browser)
        except (OSError, RuntimeError) as e:
            raise AuthError(str(e)) from e
        await self.ctx.add_cookies(cookies)

    async def close(self) -> None:
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    def _sniff(self, req: Request) -> None:
        if not req.url.startswith(API) or req.method != "POST":
            return
        tok = req.headers.get("x-framework-xsrf-token")
        if tok:
            self.xsrf = tok
        if self.request_header is None:
            try:
                body = json.loads(req.post_data or "null")
            except ValueError:
                return
            if isinstance(body, list) and len(body) >= 100 and isinstance(body[99], list):
                self.request_header = body[99]
        if self.xsrf and self.request_header is not None:
            self._ready.set()

    # ---- API ---------------------------------------------------------------
    async def call(self, method: str, body: list, space_id: str | None = None) -> list:
        """POST a pblite request to /api/<method>; return the decoded response array."""
        headers = {"content-type": "application/json", "x-framework-xsrf-token": self.xsrf}
        if space_id:
            headers["x-goog-chat-space-id"] = space_id
        res = await self.page.evaluate(
            """async ([url, headers, body]) => {
                const r = await fetch(url, {method: 'POST', headers, body, credentials: 'include'});
                return [r.status, await r.text()];
            }""",
            [f"{API}{method}", headers, json.dumps(body)],
        )
        status, text = res
        if status in (401, 403):
            raise AuthError(f"{method}: HTTP {status}")
        if status != 200:
            raise RuntimeError(f"{method}: HTTP {status}: {text[:300]}")
        if text.startswith(")]}'"):
            text = text[4:]
        data = json.loads(text)
        # Envelope: [["dfe.xxx", field1, field2, ...]]. Drop the tag so field N is at N-1,
        # same as every other (untagged) message.
        return data[0][1:]

    def req(self, fields: dict[int, object]) -> list:
        """Build a pblite request array (field N at index N-1) with header at field 100."""
        arr: list = [None] * 100
        for n, v in fields.items():
            arr[n - 1] = v
        arr[99] = self.request_header
        return arr
