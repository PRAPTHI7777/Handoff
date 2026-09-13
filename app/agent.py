import asyncio
import logging
from typing import Any, Optional
from uuid import uuid4

from playwright.async_api import Playwright

from app.browser.anakin import create_browser_session
from app.browser.session import BrowserSession
from app.browser.tools import dispatch_tool, pending_from_pause
from app.config import settings
from app.llm import GeminiToolClient, function_response_content, truncate_obs, user_text_content
from app.prompts import SYSTEM_PROMPT, build_user_task_message, profile_lines
from app.schemas import Observation, ResumeRequest, TaskCreate, TaskStatus, ToolResult

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {
    TaskStatus.running,
    TaskStatus.needs_info,
    TaskStatus.needs_confirmation,
    TaskStatus.needs_human,
}


class AgentTask:
    def __init__(self, spec: TaskCreate):
        self.id = uuid4().hex[:12]
        self.goal = spec.goal
        self.start_url = spec.start_url
        self.profile = spec.profile
        self.status = TaskStatus.running
        self.step = 0
        self.events: list[dict[str, Any]] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.resume_event = asyncio.Event()
        self.resume_payload: Optional[ResumeRequest] = None
        self.screenshot: Optional[bytes] = None
        self.backend = ""
        self.summary = ""
        self.error = ""
        self._session: Optional[BrowserSession] = None
        self._pending_pause: dict[str, Any] = {}

    def emit(self, event: dict[str, Any]) -> None:
        event = {"task_id": self.id, **event}
        self.events.append(event)
        self.queue.put_nowait(event)


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, AgentTask] = {}
        self._playwright: Optional[Playwright] = None
        self._llm: Optional[GeminiToolClient] = None

    def attach(self, playwright: Playwright) -> None:
        self._playwright = playwright

    def _ensure_llm(self) -> GeminiToolClient:
        if self._llm is None:
            self._llm = GeminiToolClient()
        return self._llm

    def get(self, task_id: str) -> AgentTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def has_active(self) -> bool:
        return any(t.status in ACTIVE_STATUSES for t in self.tasks.values())

    async def start_task(self, spec: TaskCreate) -> AgentTask:
        if self._playwright is None:
            raise RuntimeError("Task manager is not ready")
        self._ensure_llm()
        if self.has_active():
            raise RuntimeError("A task is already active. Wait for it to finish or fail.")
        task = AgentTask(spec)
        self.tasks[task.id] = task
        asyncio.create_task(self._run(task), name=f"handoff-{task.id}")
        return task

    def resume(self, task_id: str, payload: ResumeRequest) -> AgentTask:
        task = self.get(task_id)
        if task.status not in {
            TaskStatus.needs_info,
            TaskStatus.needs_confirmation,
            TaskStatus.needs_human,
        }:
            raise RuntimeError(f"Task is not paused (status={task.status.value})")
        task.resume_payload = payload
        task.resume_event.set()
        return task

    async def _run(self, task: AgentTask) -> None:
        assert self._playwright is not None
        assert self._llm is not None
        session: Optional[BrowserSession] = None
        try:
            session = await create_browser_session(self._playwright)
            task._session = session
            task.backend = getattr(session, "backend", "")
            task.emit({"type": "status", "status": task.status.value, "backend": task.backend})

            if task.start_url:
                obs = await session.navigate(task.start_url)
                await self._after_browser(task, session, "navigate", {"url": task.start_url}, obs)
            else:
                obs = await session.snapshot()
                await self._after_browser(task, session, "snapshot", {}, obs)

            profile = profile_lines(task.profile.name, task.profile.email, task.profile.phone)
            contents = [
                user_text_content(
                    build_user_task_message(task.goal, task.start_url, profile)
                    + "\n\nCurrent page:\n"
                    + truncate_obs(obs.as_prompt())
                )
            ]

            while task.step < settings.max_steps and task.status == TaskStatus.running:
                task.step += 1
                try:
                    name, args, model_content = await self._llm.next_tool(contents, SYSTEM_PROMPT)
                except Exception as exc:
                    logger.exception("Gemini tool call failed")
                    task.status = TaskStatus.failed
                    task.error = f"LLM error: {exc}"
                    task.emit({"type": "failed", "status": "failed", "reason": task.error})
                    return

                task.emit({"type": "tool", "step": task.step, "tool": name, "args": _safe_args(name, args)})
                contents.append(model_content)

                result = await dispatch_tool(session, name, args)
                result = await self._handle_result(task, session, name, args, result, contents)
                if task.status in {TaskStatus.completed, TaskStatus.failed}:
                    return
                if result is None:
                    continue

            if task.status == TaskStatus.running:
                task.status = TaskStatus.failed
                task.error = f"Stopped after {settings.max_steps} steps without completing."
                task.emit({"type": "failed", "status": "failed", "reason": task.error})
        except Exception as exc:
            logger.exception("Agent loop crashed")
            task.status = TaskStatus.failed
            task.error = str(exc)
            task.emit({"type": "failed", "status": "failed", "reason": task.error})
        finally:
            if session is not None:
                await session.close()
            task._session = None

    async def _handle_result(
        self,
        task: AgentTask,
        session: BrowserSession,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        contents: list,
    ) -> Optional[ToolResult]:
        if result.kind == "complete":
            if not result.evidence.strip():
                obs = await session.snapshot()
                msg = "complete rejected: evidence is required from the current page."
                obs.error = msg
                await self._after_browser(task, session, name, args, obs)
                contents.append(function_response_content(name, {"error": msg, "observation": obs.as_prompt()}))
                return result
            task.status = TaskStatus.completed
            task.summary = result.summary
            contents.append(
                function_response_content(
                    name, {"ok": True, "summary": result.summary, "evidence": result.evidence}
                )
            )
            task.emit(
                {
                    "type": "completed",
                    "status": "completed",
                    "summary": result.summary,
                    "evidence": result.evidence,
                }
            )
            return result

        if result.kind == "fail":
            task.status = TaskStatus.failed
            task.error = result.reason
            contents.append(function_response_content(name, {"ok": False, "reason": result.reason}))
            task.emit({"type": "failed", "status": "failed", "reason": result.reason})
            return result

        if result.kind == "pause":
            return await self._pause(task, session, name, result, contents)

        obs = result.observation or Observation(error="missing observation")
        await self._after_browser(task, session, name, args, obs)
        contents.append(
            function_response_content(
                name,
                {
                    "observation": truncate_obs(obs.as_prompt()),
                    "error": obs.error,
                },
            )
        )
        return result

    async def _pause(
        self,
        task: AgentTask,
        session: BrowserSession,
        name: str,
        result: ToolResult,
        contents: list,
    ) -> Optional[ToolResult]:
        status = result.pause_status or TaskStatus.needs_info
        payload = result.pause_payload
        task.status = status
        task._pending_pause = payload
        task.resume_event.clear()
        task.resume_payload = None
        await self._store_screenshot(task, session)
        task.emit(
            {
                "type": "pause",
                "status": status.value,
                "tool": name,
                "payload": payload,
            }
        )
        await task.resume_event.wait()
        resume = task.resume_payload or ResumeRequest()
        task.resume_payload = None
        task.status = TaskStatus.running
        task.emit({"type": "status", "status": "running", "resumed_from": status.value})

        extra = ""
        follow: Optional[ToolResult] = None

        if status == TaskStatus.needs_confirmation:
            if resume.confirmed:
                pending = pending_from_pause(payload)
                if pending:
                    tool_name, tool_args = pending
                    follow = await dispatch_tool(session, tool_name, tool_args, confirmed=True)
                    extra = "User confirmed. Consequential action was executed."
                else:
                    extra = "User confirmed. Continue with the next safe step."
            else:
                extra = "User declined confirmation. Do not perform that action. Continue or fail."
        elif status == TaskStatus.needs_info:
            extra = f"User answers: {resume.answers or resume.message or '(empty)'}"
        else:
            extra = "User signaled that the human step is done. Snapshot the page and continue."
            if resume.message:
                extra += f" Note: {resume.message}"

        obs = await session.snapshot()
        await self._after_browser(task, session, "snapshot", {}, obs)

        if follow and follow.kind == "observation" and follow.observation:
            obs = follow.observation
            extra += "\nAction after confirmation completed."
            await self._after_browser(task, session, payload.get("pending_tool") or "click", payload.get("pending_args") or {}, obs)

        contents.append(
            function_response_content(
                name,
                {
                    "paused": True,
                    "resume": extra,
                    "observation": truncate_obs(obs.as_prompt()),
                },
            )
        )
        return follow or result

    async def _after_browser(
        self,
        task: AgentTask,
        session: BrowserSession,
        tool: str,
        args: dict[str, Any],
        obs: Observation,
    ) -> None:
        await self._store_screenshot(task, session)
        task.emit(
            {
                "type": "observation",
                "step": task.step,
                "tool": tool,
                "args": _safe_args(tool, args),
                "url": obs.url,
                "title": obs.title,
                "refs_text": obs.refs_text,
                "page_text": obs.page_text[:800],
                "error": obs.error,
            }
        )

    async def _store_screenshot(self, task: AgentTask, session: BrowserSession) -> None:
        png = await session.screenshot_png()
        if png:
            task.screenshot = png
            task.emit({"type": "screenshot", "step": task.step})


def _safe_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "type" and "text" in args:
        text = str(args.get("text", ""))
        shown = text if len(text) <= 80 else text[:80] + "…"
        return {**args, "text": shown}
    return args


manager = TaskManager()
