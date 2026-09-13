import logging
from typing import Optional

from playwright.async_api import Browser, Page, Playwright, TimeoutError as PlaywrightTimeout

from app.browser.session import BrowserSession
from app.browser.snapshot import capture_snapshot
from app.config import settings
from app.schemas import Observation, RefInfo

logger = logging.getLogger(__name__)

ALLOWED_KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowDown",
    "ArrowUp",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageDown",
    "PageUp",
    "Space",
}


class PlaywrightBrowserSession(BrowserSession):
    def __init__(self, playwright: Playwright):
        self._playwright = playwright
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._refs: dict[int, RefInfo] = {}
        self._using_anakin = False
        self.backend: str = "unstarted"

    async def start(self) -> None:
        mode = settings.browser_mode
        use_anakin = mode == "anakin" or (mode == "auto" and bool(settings.anakin_api_key))
        if mode == "local":
            use_anakin = False

        if use_anakin:
            try:
                await self._connect_anakin()
                return
            except Exception:
                logger.exception("Anakin CDP connect failed; falling back to local Playwright")
                if mode == "anakin":
                    raise
        await self._launch_local()

    async def _connect_anakin(self) -> None:
        if not settings.anakin_api_key:
            raise RuntimeError("ANAKIN_API_KEY is required for Anakin browser mode")
        logger.info("Connecting to Anakin Browser API via CDP")
        browser = await self._playwright.chromium.connect_over_cdp(
            settings.anakin_cdp_url,
            headers={"X-API-Key": settings.anakin_api_key},
        )
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        self._browser = browser
        self._page = page
        self._using_anakin = True
        self.backend = "anakin"

    async def _launch_local(self) -> None:
        logger.info("Launching local Chromium (headless=%s)", settings.browser_headless)
        browser = await self._playwright.chromium.launch(headless=settings.browser_headless)
        page = await browser.new_page()
        self._browser = browser
        self._page = page
        self._using_anakin = False
        self.backend = "local"

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser session is not started")
        return self._page

    async def close(self) -> None:
        browser = self._browser
        self._browser = None
        self._page = None
        self._refs = {}
        if browser is None:
            return
        try:
            if self._using_anakin:
                await browser.close()
            else:
                await browser.close()
        except Exception:
            logger.exception("Error closing browser")

    async def _observe(self, error: Optional[str] = None) -> Observation:
        page = self._require_page()
        try:
            observation, refs = await capture_snapshot(page)
        except Exception as exc:
            logger.exception("Snapshot failed")
            observation = Observation(
                url=page.url,
                title="",
                refs_text="(snapshot failed)",
                error=str(exc),
            )
            refs = {}
        self._refs = refs
        if error:
            observation.error = error
        return observation

    async def _settle(self) -> None:
        page = self._require_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeout:
            pass
        try:
            await page.wait_for_timeout(400)
        except Exception:
            pass

    async def navigate(self, url: str) -> Observation:
        page = self._require_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return await self._observe(error=f"navigate failed: {exc}")
        await self._settle()
        return await self._observe()

    async def snapshot(self) -> Observation:
        await self._settle()
        return await self._observe()

    def _locator(self, ref: int):
        if ref not in self._refs:
            raise ValueError(f"Unknown ref [{ref}]. Call snapshot and use a listed number.")
        return self._require_page().locator(f"[data-handoff-ref='{ref}']").first

    async def click(self, ref: int) -> Observation:
        try:
            locator = self._locator(ref)
            await locator.click(timeout=8000)
        except Exception as exc:
            return await self._observe(error=f"click [{ref}] failed: {exc}")
        await self._settle()
        return await self._observe()

    async def type_text(self, ref: int, text: str) -> Observation:
        try:
            locator = self._locator(ref)
            await locator.click(timeout=8000)
            await locator.fill(text, timeout=8000)
        except Exception as exc:
            return await self._observe(error=f"type [{ref}] failed: {exc}")
        await self._settle()
        return await self._observe()

    async def select(self, ref: int, value: str) -> Observation:
        try:
            locator = self._locator(ref)
            await locator.select_option(value, timeout=8000)
        except Exception as exc:
            try:
                locator = self._locator(ref)
                await locator.select_option(label=value, timeout=8000)
            except Exception as inner:
                return await self._observe(error=f"select [{ref}] failed: {inner or exc}")
        await self._settle()
        return await self._observe()

    async def press_key(self, key: str) -> Observation:
        key = key.strip()
        if key not in ALLOWED_KEYS:
            return await self._observe(
                error=f"unsupported key '{key}'. Allowed: {', '.join(sorted(ALLOWED_KEYS))}"
            )
        page = self._require_page()
        try:
            await page.keyboard.press(" " if key == "Space" else key)
        except Exception as exc:
            return await self._observe(error=f"press_key failed: {exc}")
        await self._settle()
        return await self._observe()

    async def screenshot_png(self) -> Optional[bytes]:
        page = self._page
        if page is None:
            return None
        try:
            return await page.screenshot(type="png", full_page=False)
        except Exception:
            logger.exception("Screenshot failed")
            return None

    def ref_info(self, ref: int) -> Optional[RefInfo]:
        return self._refs.get(ref)

    def known_refs(self) -> set[int]:
        return set(self._refs)

    def current_url(self) -> str:
        return self._page.url if self._page is not None else ""


async def create_browser_session(playwright: Playwright) -> BrowserSession:
    session = PlaywrightBrowserSession(playwright)
    await session.start()
    return session
