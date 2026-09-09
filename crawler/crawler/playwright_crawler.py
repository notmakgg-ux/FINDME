"""
Playwright-based JavaScript rendering fallback crawler.
Only used when HTTP extraction fails on important pages.
"""

import asyncio
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_playwright = None
_browser = None


async def _ensure_browser():
    """Lazily initialize Playwright browser."""
    global _playwright, _browser

    if _browser is not None:
        return _browser

    try:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )
        return _browser
    except Exception as e:
        logger.warning(f"Failed to initialize Playwright: {e}")
        return None


async def render_page(url: str, wait_ms: int = 3000) -> Optional[str]:
    """Render a page with Playwright and return the full HTML."""
    browser = await _ensure_browser()
    if browser is None:
        return None

    context = None
    page = None

    try:
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Wait for content to settle
        await asyncio.sleep(wait_ms / 1000)

        html = await page.content()
        return html

    except Exception as e:
        logger.debug(f"Playwright failed to render {url}: {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass


async def render_pages(urls: list[str], wait_ms: int = 3000) -> dict[str, Optional[str]]:
    """Render multiple pages with Playwright."""
    results = {}
    for url in urls:
        html = await render_page(url, wait_ms)
        results[url] = html
    return results


async def close_browser():
    """Clean up Playwright resources."""
    global _playwright, _browser

    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None

    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
