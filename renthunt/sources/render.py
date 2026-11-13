"""Optional browser rendering for pages that build their listings with JavaScript.

Needs the `playwright` package and its Chromium build, neither of which RentHunt installs
by default:

    .venv/bin/pip install playwright        (or: uv pip install --python .venv/bin/python playwright)
    .venv/bin/python -m playwright install chromium

Without them, pages set to use the browser report that it is not installed and everything
else keeps working. The browser is an ordinary headless Chromium: it does nothing to get
past CAPTCHAs or bot checks, and a page that shows one is reported as blocked.
"""
from __future__ import annotations

import importlib.util

from .. import config


class RenderUnavailable(RuntimeError):
    """Playwright or its browser is not installed."""


class RenderIncomplete(RuntimeError):
    """The page did not finish loading in the browser."""


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class Browser:
    """One headless Chromium for the length of a hunt, started on first use."""

    def __init__(self, timeout_seconds: float = 30):
        self.timeout_ms = int(timeout_seconds * 1000)
        self._play = self._browser = None

    def _start(self):
        if self._browser:
            return
        if not available():
            raise RenderUnavailable("browser rendering is not installed (see README: Optional browser rendering)")
        from playwright.sync_api import sync_playwright
        try:
            self._play = sync_playwright().start()
            self._browser = self._play.chromium.launch(headless=True)
        except Exception as exc:
            self.close()
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise RenderUnavailable(f"the browser could not start: {first[:200]}") from exc

    def fetch(self, url: str) -> tuple[int, str, str]:
        """(HTTP status, final URL, HTML after scripts ran, with the content of any frames)."""
        self._start()
        from playwright.sync_api import Error, TimeoutError as PlayTimeout
        context = self._browser.new_context(user_agent=config.USER_AGENT, locale="en-US")
        try:
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except PlayTimeout as exc:
                raise RenderIncomplete("the page did not load in the browser in time") from exc
            except Error as exc:
                raise RuntimeError(f"browser: {str(exc).splitlines()[0][:200]}") from exc
            try:  # let scripts fetch their data; busy pages never go fully idle, which is fine
                page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 12000))
            except PlayTimeout:
                pass
            page.wait_for_timeout(500)
            html = page.content()
            for frame in page.frames[1:]:  # listing widgets are often embedded in a frame
                try:
                    if frame.url.startswith("http"):
                        html += f"\n<!-- frame {frame.url} -->\n" + frame.content()
                except Error:
                    continue
            return (response.status if response else 0), page.url, html
        finally:
            context.close()

    def close(self) -> None:
        for thing, stop in ((self._browser, "close"), (self._play, "stop")):
            try:
                if thing:
                    getattr(thing, stop)()
            except Exception:
                pass
        self._play = self._browser = None
