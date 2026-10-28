import unittest
from pathlib import Path
from playwright.async_api import async_playwright

from app.browser.anakin import PlaywrightBrowserSession
from app.browser.tools import dispatch_tool
from app.schemas import TaskStatus


@unittest.skipUnless(
    Path(__import__("app.config", fromlist=["settings"]).settings.local_browser_executable).is_file(),
    "Configured local browser executable is unavailable",
)
class BrowserToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_requires_confirmation_before_consequential_form_submit(self) -> None:
        executable = Path(__import__("app.config", fromlist=["settings"]).settings.local_browser_executable)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = await browser.new_page()
            await page.set_content(
                """<!doctype html><form id="order">
                  <label>Quantity <input id="quantity" type="text"></label>
                  <button type="submit">Place order</button>
                </form><script>
                  window.submits = 0;
                  document.querySelector('form').addEventListener('submit', event => {
                    event.preventDefault(); window.submits++;
                  });
                </script>"""
            )
            session = PlaywrightBrowserSession(playwright)
            session._browser = browser
            session._page = page
            try:
                await page.locator("#quantity").focus()
                blocked = await dispatch_tool(session, "press_key", {"key": "Enter"})
                self.assertEqual(blocked.kind, "pause")
                self.assertEqual(blocked.pause_status, TaskStatus.needs_confirmation)
                self.assertEqual(await page.evaluate("window.submits"), 0)

                approved = await dispatch_tool(
                    session, "press_key", {"key": "Enter"}, confirmed=True
                )
                self.assertEqual(approved.kind, "observation")
                self.assertEqual(await page.evaluate("window.submits"), 1)
            finally:
                await browser.close()

    async def test_enter_does_not_pause_for_non_consequential_form(self) -> None:
        executable = Path(__import__("app.config", fromlist=["settings"]).settings.local_browser_executable)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = await browser.new_page()
            await page.set_content(
                """<form><label>Search <input id="query"></label><button>Search</button></form>"""
            )
            session = PlaywrightBrowserSession(playwright)
            session._browser = browser
            session._page = page
            try:
                await page.locator("#query").focus()
                result = await dispatch_tool(session, "press_key", {"key": "Enter"})
                self.assertEqual(result.kind, "observation")
            finally:
                await browser.close()
