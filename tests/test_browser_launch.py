import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.browser.anakin import PlaywrightBrowserSession
from app.config import settings


class BrowserLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_mode_uses_configured_executable_and_headless_setting(self) -> None:
        browser = Mock()
        page = Mock()
        browser.new_page = AsyncMock(return_value=page)
        chromium = Mock()
        chromium.launch = AsyncMock(return_value=browser)
        playwright = Mock(chromium=chromium)
        session = PlaywrightBrowserSession(playwright)

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "chrome.exe"
            executable.touch()
            with (
                patch.object(settings, "browser_mode", "local"),
                patch.object(settings, "local_browser_executable", str(executable)),
                patch.object(settings, "browser_headless", True),
            ):
                await session._launch_local()

        chromium.launch.assert_awaited_once_with(
            headless=True, executable_path=str(executable)
        )
        self.assertIs(session._page, page)
        self.assertEqual(session.backend, "local")

    async def test_local_mode_closes_browser_when_page_initialization_fails(self) -> None:
        startup_error = ValueError("controlled page initialization failure")
        browser = Mock()
        browser.new_page = AsyncMock(side_effect=startup_error)
        browser.close = AsyncMock()
        chromium = Mock()
        chromium.launch = AsyncMock(return_value=browser)
        session = PlaywrightBrowserSession(Mock(chromium=chromium))

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "chrome.exe"
            executable.touch()
            with (
                patch.object(settings, "browser_mode", "local"),
                patch.object(settings, "local_browser_executable", str(executable)),
                self.assertRaises(ValueError) as raised,
            ):
                await session._launch_local()

        self.assertIs(raised.exception, startup_error)
        browser.close.assert_awaited_once_with()
        self.assertIsNone(session._browser)
        self.assertIsNone(session._page)

    async def test_anakin_mode_closes_connection_when_context_creation_fails(self) -> None:
        startup_error = ValueError("controlled context initialization failure")
        browser = Mock(contexts=[])
        browser.new_context = AsyncMock(side_effect=startup_error)
        browser.close = AsyncMock()
        chromium = Mock()
        chromium.connect_over_cdp = AsyncMock(return_value=browser)
        session = PlaywrightBrowserSession(Mock(chromium=chromium))

        with (
            patch.object(settings, "anakin_api_key", "dummy-anakin-key"),
            patch.object(settings, "anakin_cdp_url", "ws://127.0.0.1/dummy"),
            self.assertRaises(ValueError) as raised,
        ):
            await session._connect_anakin()

        self.assertIs(raised.exception, startup_error)
        browser.close.assert_awaited_once_with()
        self.assertIsNone(session._browser)
        self.assertIsNone(session._page)

    async def test_anakin_mode_closes_new_context_and_connection_when_page_fails(self) -> None:
        startup_error = ValueError("controlled page initialization failure")
        context = Mock(pages=[])
        context.new_page = AsyncMock(side_effect=startup_error)
        context.close = AsyncMock()
        browser = Mock(contexts=[])
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        chromium = Mock()
        chromium.connect_over_cdp = AsyncMock(return_value=browser)
        session = PlaywrightBrowserSession(Mock(chromium=chromium))

        with (
            patch.object(settings, "anakin_api_key", "dummy-anakin-key"),
            patch.object(settings, "anakin_cdp_url", "ws://127.0.0.1/dummy"),
            self.assertRaises(ValueError) as raised,
        ):
            await session._connect_anakin()

        self.assertIs(raised.exception, startup_error)
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()
        self.assertIsNone(session._browser)
        self.assertIsNone(session._page)

    async def test_local_mode_reports_missing_configured_executable(self) -> None:
        chromium = Mock()
        chromium.launch = AsyncMock()
        session = PlaywrightBrowserSession(Mock(chromium=chromium))
        missing = Path(tempfile.gettempdir()) / "handoff-missing-chrome.exe"

        with (
            patch.object(settings, "browser_mode", "local"),
            patch.object(settings, "local_browser_executable", str(missing)),
            self.assertRaisesRegex(
                FileNotFoundError,
                r"LOCAL_BROWSER_EXECUTABLE.*existing browser executable",
            ),
        ):
            await session._launch_local()

        chromium.launch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
