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
