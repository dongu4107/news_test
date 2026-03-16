from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class RenderedPage:
    final_url: str
    html: str


class RenderContext:
    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
    ):
        self._max_concurrency = max(1, max_concurrency)
        self._user_agent = user_agent
        self._accept_language = accept_language
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._pw = None
        self._browser = None

    @staticmethod
    async def create(
        *, max_concurrency: int = 2, user_agent: Optional[str] = None, accept_language: Optional[str] = None
    ) -> "RenderContext":
        ctx = RenderContext(max_concurrency=max_concurrency, user_agent=user_agent, accept_language=accept_language)
        await ctx._start()
        return ctx

    async def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Playwright not installed. Install with `pip install -e '.[render]'` and run `playwright install`."
            ) from e

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def fetch_html(self, url: str, *, timeout_ms: int = 20_000) -> RenderedPage:
        if not self._browser:
            raise RuntimeError("RenderContext not started")
        async with self._sem:
            context_kwargs = {}
            if self._user_agent:
                context_kwargs["user_agent"] = self._user_agent
            context = await self._browser.new_context(**context_kwargs)
            page = await context.new_page()
            try:
                headers: Dict[str, str] = {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    "Cache-Control": "max-age=0",
                    "Pragma": "no-cache",
                    "Upgrade-Insecure-Requests": "1",
                }
                if self._accept_language:
                    headers["Accept-Language"] = self._accept_language
                await page.set_extra_http_headers(headers)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                html = await page.content()
                final_url = page.url
                return RenderedPage(final_url=final_url, html=html)
            finally:
                await page.close()
                await context.close()
