import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.agent import TaskManager
from app.config import settings
from app.memory import MemoryStore
from app.profile import ProfileStore
from app.schemas import TaskCreate, TaskStatus


PAGE_TITLE = "Controlled Handoff Evidence"
PAGE_MARKER = "BLUE-HARBOR-481"


class ControlledPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = (
            "<!doctype html><html><head>"
            f"<title>{PAGE_TITLE}</title>"
            "</head><body><p>Visible evidence marker: "
            f"{PAGE_MARKER}</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


class ScriptedToolClient:
    def __init__(self, responses: list[tuple[str, dict[str, Any]]]) -> None:
        self._responses = responses
        self.call_count = 0
        self.observed_page_text = ""

    async def next_tool(
        self,
        contents: list[dict[str, Any]],
        _system_instruction: str,
        execution_state: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        self.call_count += 1
        observation = execution_state.split("Current observation:\n", 1)[-1]
        if "Visible text excerpt:" in observation:
            self.observed_page_text = observation.split(
                "Visible text excerpt:\n", 1
            )[1].split("\nRecent actions/results:", 1)[0]
        if PAGE_MARKER not in self.observed_page_text:
            raise AssertionError(
                "Refusing to return completion before the local page marker "
                "appears in the observed page content."
            )

        if self.call_count == 2:
            tool_results = [
                item.get("content", "")
                for item in contents
                if item.get("role") == "tool"
            ]
            if not any("complete rejected" in result for result in tool_results):
                raise AssertionError(
                    "Expected the initial single-marker completion to be "
                    "rejected before returning contextual evidence."
                )

        if not self._responses:
            raise AssertionError(
                "Scripted tool response queue exhausted after "
                f"{self.call_count - 1} responses; expected "
                "the completion rejection and retry loop."
            )

        name, args = self._responses.pop(0)
        call_id = f"controlled-{self.call_count}-{uuid4().hex}"
        raw_args = json.dumps(args)
        model_content = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_args},
                }
            ],
        }
        return name, args, model_content


@unittest.skipUnless(
    Path(settings.local_browser_executable).is_file(),
    "Configured local browser executable is unavailable",
)
class LocalBrowserWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_retries_with_page_evidence_after_rejection(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledPageHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        page_url = f"http://127.0.0.1:{server.server_port}/"

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                client = ScriptedToolClient(
                    [
                        ("complete", {"summary": "Page verified", "evidence": PAGE_MARKER}),
                        (
                            "complete",
                            {
                                "summary": "Page verified",
                                "evidence": f"Visible evidence marker {PAGE_MARKER}",
                            },
                        ),
                    ]
                )
                with (
                    patch.object(settings, "browser_mode", "local"),
                    patch.object(settings, "browser_headless", True),
                    patch.object(settings, "memory_file", temp_path / "memories.json"),
                    patch.object(settings, "profile_file", temp_path / "profile.json"),
                ):
                    async with async_playwright() as playwright:
                        manager = TaskManager()
                        manager.attach(playwright)
                        manager._llm = client
                        manager._memory_store = MemoryStore(settings.memory_file)
                        manager._profile_store = ProfileStore(settings.profile_file)

                        task = await manager.start_task(
                            TaskCreate(
                                goal="Verify the visible marker on the local test page.",
                                start_url=page_url,
                            )
                        )
                        deadline = asyncio.get_running_loop().time() + 30
                        while task.status in {
                            TaskStatus.running,
                            TaskStatus.needs_info,
                            TaskStatus.needs_confirmation,
                            TaskStatus.needs_human,
                        }:
                            if asyncio.get_running_loop().time() >= deadline:
                                self.fail(f"Task did not finish; status={task.status}")
                            await asyncio.sleep(0.05)

                        self.assertEqual(task.status, TaskStatus.completed, task.error)
                        self.assertEqual(task.backend, "local")
                        self.assertEqual(client.call_count, 2)
                        self.assertIn(PAGE_MARKER, client.observed_page_text)
                        observations = [
                            event for event in task.events
                            if event.get("type") == "observation"
                        ]
                        self.assertTrue(
                            any(
                                event.get("url") == page_url
                                and PAGE_MARKER in event.get("page_text", "")
                                for event in observations
                            ),
                            "Task did not record the navigated page and visible marker.",
                        )
                        completed = [
                            event for event in task.events
                            if event.get("type") == "completed"
                        ]
                        self.assertEqual(len(completed), 1)
                        self.assertEqual(
                            completed[0]["evidence"],
                            f"Visible evidence marker {PAGE_MARKER}",
                        )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
