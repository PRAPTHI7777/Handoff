import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from playwright.async_api import Playwright

from app.browser.anakin import create_browser_session
from app.browser.session import BrowserSession
from app.browser.tools import BROWSER_TOOLS, dispatch_tool, pending_from_pause
from app.config import settings
from app.llm import (
    GroqToolClient,
    function_response_content,
    truncate_obs,
    user_text_content,
)
from app.memory import MemoryStore, MemoryStoreError
from app.prompts import (
    SYSTEM_PROMPT,
    build_execution_state,
    build_user_task_message,
    profile_lines,
)
from app.schemas import (
    Observation,
    DeleteMemoryArgs,
    RetrieveMemoryArgs,
    ResumeRequest,
    SaveMemoryArgs,
    TaskCreate,
    TaskStatus,
    ToolResult,
)

logger = logging.getLogger(__name__)

MAX_IDENTICAL_FAILURES = 2
MEMORY_TOOLS = {"save_memory", "retrieve_memory", "delete_memory"}


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

        self.events: List[Dict[str, Any]] = []
        self.queue: asyncio.Queue = asyncio.Queue()

        self.resume_event = asyncio.Event()
        self.resume_payload: Optional[ResumeRequest] = None

        self.screenshot: Optional[bytes] = None

        self.backend = ""
        self.summary = ""
        self.error = ""

        self._session: Optional[BrowserSession] = None
        self._pending_pause: Dict[str, Any] = {}
        self.plan = ""
        self.current_observation = Observation()
        self.recent_actions: List[str] = []
        self.failed_actions: Dict[str, int] = {}
        self._plan_page = ""
        self.relevant_memories: List[Dict[str, str]] = []

    def emit(self, event: Dict[str, Any]) -> None:
        event = {
            "task_id": self.id,
            **event,
        }

        self.events.append(event)
        self.queue.put_nowait(event)


class TaskManager:
    def __init__(self) -> None:
        self.tasks: Dict[str, AgentTask] = {}
        self._playwright: Optional[Playwright] = None
        self._llm: Optional[GroqToolClient] = None
        self._memory_store = MemoryStore(settings.memory_file)

    def attach(self, playwright: Playwright) -> None:
        self._playwright = playwright

    def _ensure_llm(self) -> GroqToolClient:
        if self._llm is None:
            self._llm = GroqToolClient()

        return self._llm

    def get(self, task_id: str) -> AgentTask:
        task = self.tasks.get(task_id)

        if task is None:
            raise KeyError(task_id)

        return task

    def has_active(self) -> bool:
        return any(
            t.status in ACTIVE_STATUSES
            for t in self.tasks.values()
        )

    async def start_task(self, spec: TaskCreate) -> AgentTask:

        if self._playwright is None:
            raise RuntimeError("Task manager is not ready")

        self._ensure_llm()

        if self.has_active():
            raise RuntimeError(
                "A task is already active. "
                "Wait for it to finish or fail."
            )

        task = AgentTask(spec)

        self.tasks[task.id] = task

        asyncio.create_task(
            self._run(task),
            name="handoff-" + task.id,
        )

        return task

    def resume(
        self,
        task_id: str,
        payload: ResumeRequest,
    ) -> AgentTask:

        task = self.get(task_id)

        if task.status not in {
            TaskStatus.needs_info,
            TaskStatus.needs_confirmation,
            TaskStatus.needs_human,
        }:
            raise RuntimeError(
                "Task is not paused "
                f"(status={task.status.value})"
            )

        task.resume_payload = payload
        task.resume_event.set()

        return task

    async def _run(self, task: AgentTask) -> None:

        assert self._playwright is not None
        assert self._llm is not None

        session: Optional[BrowserSession] = None

        try:
            session = await create_browser_session(
                self._playwright
            )

            task._session = session
            task.backend = getattr(session, "backend", "")

            task.emit(
                {
                    "type": "status",
                    "status": task.status.value,
                    "backend": task.backend,
                }
            )

            if task.start_url:
                obs = await session.navigate(
                    task.start_url
                )

                await self._after_browser(
                    task,
                    session,
                    "navigate",
                    {"url": task.start_url},
                    obs,
                )
            else:
                obs = await session.snapshot()

                await self._after_browser(
                    task,
                    session,
                    "snapshot",
                    {},
                    obs,
                )

            profile = profile_lines(
                task.profile.name,
                task.profile.email,
                task.profile.phone,
            )

            task.relevant_memories = self._relevant_memories(task.goal)

            initial_message = (
                build_user_task_message(
                    task.goal,
                    task.start_url,
                    profile,
                )
                + "\n\nCurrent page:\n"
                + truncate_obs(obs.as_prompt())
            )

            contents: List[Dict[str, Any]] = [
                user_text_content(initial_message)
            ]

            self._revise_plan(task, obs, "initial page")

            while (
                task.step < settings.max_steps
                and task.status == TaskStatus.running
            ):

                task.step += 1

                try:
                    (
                        name,
                        args,
                        model_content,
                    ) = await self._llm.next_tool(
                        contents,
                        SYSTEM_PROMPT,
                        build_execution_state(
                            task.goal,
                            task.plan,
                            task.current_observation,
                            task.recent_actions,
                            [item["value"] for item in task.relevant_memories],
                        ),
                    )

                except Exception as exc:

                    logger.exception(
                        "Groq tool call failed"
                    )

                    task.status = TaskStatus.failed
                    task.error = f"LLM error: {exc}"

                    task.emit(
                        {
                            "type": "failed",
                            "status": "failed",
                            "reason": task.error,
                        }
                    )

                    return

                task.emit(
                    {
                        "type": "tool",
                        "step": task.step,
                        "tool": name,
                        "args": _safe_args(name, args),
                    }
                )

                # Save assistant tool call.
                contents.append(model_content)

                tool_call_id = (
                    model_content["tool_calls"][0]["id"]
                )

                memory_response = self._run_memory_tool(
                    task,
                    name,
                    args,
                )
                if memory_response is not None:
                    contents.append(
                        function_response_content(
                            name,
                            memory_response,
                            tool_call_id,
                        )
                    )
                    continue

                signature = _tool_signature(name, args)
                if task.failed_actions.get(signature, 0) >= MAX_IDENTICAL_FAILURES:
                    reason = (
                        "Stopped because "
                        f"{name} with the same arguments failed "
                        f"{MAX_IDENTICAL_FAILURES} times. "
                        "A different recovery action is required."
                    )
                    task.status = TaskStatus.failed
                    task.error = reason
                    task.emit(
                        {
                            "type": "failed",
                            "status": "failed",
                            "reason": reason,
                        }
                    )
                    return

                result = await dispatch_tool(
                    session,
                    name,
                    args,
                )

                result = await self._handle_result(
                    task,
                    session,
                    name,
                    args,
                    result,
                    contents,
                    tool_call_id,
                )

                if task.status in {
                    TaskStatus.completed,
                    TaskStatus.failed,
                }:
                    return

                if result is None:
                    continue

            if task.status == TaskStatus.running:

                task.status = TaskStatus.failed

                task.error = (
                    f"Stopped after {settings.max_steps} "
                    "steps without completing."
                )

                task.emit(
                    {
                        "type": "failed",
                        "status": "failed",
                        "reason": task.error,
                    }
                )

        except Exception as exc:

            logger.exception(
                "Agent loop crashed"
            )

            task.status = TaskStatus.failed
            task.error = str(exc)

            task.emit(
                {
                    "type": "failed",
                    "status": "failed",
                    "reason": task.error,
                }
            )

        finally:

            if session is not None:
                await session.close()

            task._session = None

    async def _handle_result(
        self,
        task: AgentTask,
        session: BrowserSession,
        name: str,
        args: Dict[str, Any],
        result: ToolResult,
        contents: List[Dict[str, Any]],
        tool_call_id: str,
    ) -> Optional[ToolResult]:

        if result.kind == "complete":

            requires_page_evidence = bool(task.start_url) or any(
                action.split("(", 1)[0] in BROWSER_TOOLS
                for action in task.recent_actions
            )

            if requires_page_evidence and not _evidence_matches_observation(
                result.evidence,
                task.current_observation,
            ):

                obs = await session.snapshot()

                msg = (
                    "complete rejected: evidence must match the "
                    "current page's title, URL, or visible text."
                )

                obs.error = msg

                await self._after_browser(
                    task,
                    session,
                    name,
                    args,
                    obs,
                )

                contents.append(
                    function_response_content(
                        name,
                        {
                            "error": msg,
                            "observation": obs.as_prompt(),
                        },
                        tool_call_id,
                    )
                )

                return result

            task.status = TaskStatus.completed
            task.summary = result.summary

            contents.append(
                function_response_content(
                    name,
                    {
                        "ok": True,
                        "summary": result.summary,
                        "evidence": result.evidence,
                    },
                    tool_call_id,
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

            contents.append(
                function_response_content(
                    name,
                    {
                        "ok": False,
                        "reason": result.reason,
                    },
                    tool_call_id,
                )
            )

            task.emit(
                {
                    "type": "failed",
                    "status": "failed",
                    "reason": result.reason,
                }
            )

            return result

        if result.kind == "pause":

            return await self._pause(
                task,
                session,
                name,
                result,
                contents,
                tool_call_id,
            )

        obs = (
            result.observation
            or Observation(error="missing observation")
        )

        await self._after_browser(
            task,
            session,
            name,
            args,
            obs,
        )

        self._record_action_result(task, name, args, obs)

        contents.append(
            function_response_content(
                name,
                {
                    "observation": truncate_obs(
                        obs.as_prompt()
                    ),
                    "error": obs.error,
                },
                tool_call_id,
            )
        )

        return result

    def _relevant_memories(
        self,
        query: str,
    ) -> List[Dict[str, str]]:
        try:
            return self._memory_store.relevant(query)
        except MemoryStoreError:
            logger.exception("Could not retrieve local memories")
            return []

    def _run_memory_tool(
        self,
        task: AgentTask,
        name: str,
        raw_args: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if name not in MEMORY_TOOLS:
            return None

        try:
            if name == "save_memory":
                args = SaveMemoryArgs.model_validate(raw_args)
                if not _explicit_memory_request(
                    task.goal,
                    {"remember", "save", "store"},
                ):
                    return {
                        "ok": False,
                        "error": (
                            "Memory was not saved: the current user did not "
                            "explicitly ask Handoff to remember it."
                        ),
                    }
                item = self._memory_store.save(args.memory)
                self._add_relevant_memory(task, item)
                task.recent_actions.append("save_memory -> saved explicit memory")
                task.emit(
                    {
                        "type": "memory",
                        "action": "saved",
                        "memory_id": item["id"],
                    }
                )
                return {"ok": True, "memory": _memory_view(item)}

            if name == "retrieve_memory":
                args = RetrieveMemoryArgs.model_validate(raw_args)
                memories = self._relevant_memories(args.query)
                for item in memories:
                    self._add_relevant_memory(task, item)
                task.recent_actions.append(
                    "retrieve_memory -> " + str(len(memories)) + " relevant memories"
                )
                return {
                    "ok": True,
                    "memories": [_memory_view(item) for item in memories],
                }

            args = DeleteMemoryArgs.model_validate(raw_args)
            if not _explicit_memory_request(
                task.goal,
                {"forget", "delete", "remove"},
            ):
                return {
                    "ok": False,
                    "error": (
                        "Memory was not deleted: the current user did not "
                        "explicitly ask Handoff to forget it."
                    ),
                }
            deleted = self._memory_store.delete(args.memory_id)
            if deleted:
                task.relevant_memories = [
                    item for item in task.relevant_memories
                    if item["id"] != args.memory_id
                ]
                task.recent_actions.append("delete_memory -> deleted explicit memory")
                task.emit(
                    {
                        "type": "memory",
                        "action": "deleted",
                        "memory_id": args.memory_id,
                    }
                )
            return {"ok": deleted, "memory_id": args.memory_id}
        except (MemoryStoreError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _add_relevant_memory(
        task: AgentTask,
        item: Dict[str, str],
    ) -> None:
        if not any(existing["id"] == item["id"] for existing in task.relevant_memories):
            task.relevant_memories.append(item)
            task.relevant_memories = task.relevant_memories[-3:]

    async def _pause(
        self,
        task: AgentTask,
        session: BrowserSession,
        name: str,
        result: ToolResult,
        contents: List[Dict[str, Any]],
        tool_call_id: str,
    ) -> Optional[ToolResult]:

        status = (
            result.pause_status
            or TaskStatus.needs_info
        )

        payload = result.pause_payload

        task.status = status
        task._pending_pause = payload

        task.resume_event.clear()
        task.resume_payload = None

        await self._store_screenshot(
            task,
            session,
        )

        task.emit(
            {
                "type": "pause",
                "status": status.value,
                "tool": name,
                "payload": payload,
            }
        )

        await task.resume_event.wait()

        resume = (
            task.resume_payload
            or ResumeRequest()
        )

        task.resume_payload = None
        task.status = TaskStatus.running

        task.emit(
            {
                "type": "status",
                "status": "running",
                "resumed_from": status.value,
            }
        )

        extra = ""
        follow: Optional[ToolResult] = None
        follow_name = ""
        follow_args: Dict[str, Any] = {}

        if status == TaskStatus.needs_confirmation:

            if resume.confirmed:

                pending = pending_from_pause(
                    payload
                )

                if pending:

                    tool_name, tool_args = pending

                    follow_name = tool_name
                    follow_args = tool_args

                    follow = await dispatch_tool(
                        session,
                        tool_name,
                        tool_args,
                        confirmed=True,
                    )

                    extra = (
                        "User confirmed. "
                        "Consequential action was executed."
                    )

                else:
                    extra = (
                        "User confirmed. "
                        "Continue with the next safe step."
                    )

            else:
                extra = (
                    "User declined confirmation. "
                    "Do not perform that action. "
                    "Continue or fail."
                )

        elif status == TaskStatus.needs_info:

            extra = (
                "User answers: "
                + str(
                    resume.answers
                    or resume.message
                    or "(empty)"
                )
            )

        else:

            extra = (
                "User signaled that the human step "
                "is done. Snapshot the page and continue."
            )

        if resume.message:
            extra += (
                " Note: "
                + resume.message
            )

        obs = await session.snapshot()

        await self._after_browser(
            task,
            session,
            "snapshot",
            {},
            obs,
        )

        if (
            follow
            and follow.kind == "observation"
            and follow.observation
        ):
            obs = follow.observation

            await self._after_browser(
                task,
                session,
                follow_name,
                follow_args,
                obs,
            )
            self._record_action_result(
                task,
                follow_name,
                follow_args,
                obs,
            )

            extra += (
                "\nAction after confirmation completed."
            )

        contents.append(
            function_response_content(
                name,
                {
                    "paused": True,
                    "resume": extra,
                    "observation": truncate_obs(
                        obs.as_prompt()
                    ),
                },
                tool_call_id,
            )
        )

        return follow or result

    async def _after_browser(
        self,
        task: AgentTask,
        session: BrowserSession,
        tool: str,
        args: Dict[str, Any],
        obs: Observation,
    ) -> None:

        self._revise_plan(task, obs, tool)

        await self._store_screenshot(
            task,
            session,
        )

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

    def _revise_plan(
        self,
        task: AgentTask,
        obs: Observation,
        trigger: str,
    ) -> None:
        """Maintain a compact working plan from the latest page state."""
        page = (obs.url + " | " + obs.title).strip(" |")
        page_changed = bool(task._plan_page and page != task._plan_page)
        task.current_observation = obs
        task._plan_page = page

        if obs.error:
            next_step = (
                "Recovery: the last action failed; use the current refs and "
                "take a different safe action."
            )
        elif page_changed:
            next_step = (
                "Revised: the page changed; reassess the current refs before "
                "continuing toward the goal."
            )
        elif trigger == "initial page":
            next_step = (
                "Inspect the current refs and take the first safe step toward "
                "the goal."
            )
        else:
            next_step = (
                "Continue from the latest observation; verify progress before "
                "any consequential action or completion."
            )

        task.plan = (
            "Goal focus: "
            + task.goal.strip()[:260]
            + ". Current page: "
            + _short_page(obs)
            + ". "
            + next_step
        )

    def _record_action_result(
        self,
        task: AgentTask,
        name: str,
        args: Dict[str, Any],
        obs: Observation,
    ) -> None:
        signature = _tool_signature(name, args)
        shown_args = json.dumps(_safe_args(name, args), sort_keys=True)
        outcome = "ok"
        if obs.error:
            count = task.failed_actions.get(signature, 0) + 1
            task.failed_actions[signature] = count
            outcome = "failed: " + obs.error
        else:
            task.failed_actions.pop(signature, None)
        task.recent_actions.append(
            name + "(" + shown_args[:160] + ") -> " + outcome
        )
        task.recent_actions = task.recent_actions[-4:]

    async def _store_screenshot(
        self,
        task: AgentTask,
        session: BrowserSession,
    ) -> None:

        png = await session.screenshot_png()

        if png:
            task.screenshot = png

            task.emit(
                {
                    "type": "screenshot",
                    "step": task.step,
                }
            )




def _memory_view(item: Dict[str, str]) -> Dict[str, str]:
    """Return the safe public representation of a stored memory."""
    return {
        "id": item["id"],
        "value": item["value"],
        "created_at": item["created_at"],
    }


def _explicit_memory_request(
    goal: str,
    keywords: set[str],
) -> bool:
    """Return True only when the user's goal explicitly requests a memory action."""
    normalized = goal.lower().strip()
    return any(keyword in normalized for keyword in keywords)

def _safe_args(
    name: str,
    args: Dict[str, Any],
) -> Dict[str, Any]:

    if name == "type" and "text" in args:

        text = str(args.get("text", ""))

        shown = (
            text
            if len(text) <= 80
            else text[:80] + "…"
        )

        return {
            **args,
            "text": shown,
        }

    return args


def _tool_signature(name: str, args: Dict[str, Any]) -> str:
    """Stable identity for detecting a repeated failed model action."""
    try:
        encoded = json.dumps(
            args or {},
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        encoded = repr(args)
    return name + ":" + encoded


def _short_page(obs: Observation) -> str:
    if obs.title:
        return obs.title[:120]
    if obs.url:
        return obs.url[:180]
    return "unknown page"


def _evidence_matches_observation(
    evidence: str,
    obs: Observation,
) -> bool:
    """Reject completion evidence that is unrelated to the latest page."""
    evidence = evidence.strip().lower()
    if not evidence:
        return False

    for value in (obs.url, obs.title):
        if value and value.lower() in evidence:
            return True

    visible = obs.page_text.lower()
    words = [
        word.strip(".,:;!?()[]{}\"'")
        for word in evidence.split()
    ]
    meaningful = [word for word in words if len(word) >= 5]
    matches = sum(1 for word in meaningful if word in visible)
    return matches >= 2


manager = TaskManager()
