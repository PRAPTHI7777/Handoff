import asyncio
import calendar
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
from uuid import uuid4

from app.agent import TaskManager
from app.schemas import (
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskStatus,
    RecurrenceKind,
    TaskCreate,
    TaskStatus,
)


RETRY_DELAY = timedelta(seconds=1)
POLL_DELAY = 0.2
logger = logging.getLogger(__name__)


class Scheduler:
    """Local JSON-backed scheduler for one-time and recurring tasks."""

    def __init__(self, manager: TaskManager, path: Optional[Path] = None) -> None:
        self.manager = manager
        self.path = path or _default_schedule_path()
        self.tasks: Dict[str, ScheduledTask] = {}
        self._retry_at: Dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._runner: Optional[asyncio.Task] = None
        self._watchers: Set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._runner is not None:
            return
        self.load()
        self._stop.clear()
        self._runner = asyncio.create_task(
            self._run(),
            name="handoff-scheduler",
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._runner is not None:
            await self._runner
            self._runner = None
        for watcher in self._watchers:
            watcher.cancel()
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)
        self._watchers.clear()

    def schedule(self, spec: ScheduledTaskCreate) -> ScheduledTask:
        scheduled = ScheduledTask(
            id=uuid4().hex[:12],
            goal=spec.goal,
            start_url=spec.start_url,
            profile=spec.profile,
            run_at=spec.run_at,
            recurrence=spec.recurrence,
            next_run_at=spec.run_at,
            status=ScheduledTaskStatus.scheduled,
        )
        with self._lock:
            self.tasks[scheduled.id] = scheduled
            self._save_locked()
        self._wake.set()
        return scheduled

    def list_tasks(self) -> List[ScheduledTask]:
        with self._lock:
            return sorted(
                self.tasks.values(),
                key=lambda task: (task.run_at, task.id),
            )

    def delete(self, scheduled_id: str) -> ScheduledTask:
        with self._lock:
            scheduled = self.tasks.get(scheduled_id)
            if scheduled is None:
                raise KeyError(scheduled_id)
            if scheduled.status != ScheduledTaskStatus.scheduled:
                raise RuntimeError("Only scheduled tasks can be deleted")
            scheduled.status = ScheduledTaskStatus.cancelled
            self._retry_at.pop(scheduled_id, None)
            self._save_locked()
        self._wake.set()
        return scheduled

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not self.path.exists():
                return
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Could not load scheduled tasks from %s: %s", self.path, exc)
                return

            records = raw.get("scheduled_tasks") if isinstance(raw, dict) else None
            if not isinstance(records, list):
                logger.error("Could not load scheduled tasks from %s: invalid JSON format", self.path)
                return

            restored: Dict[str, ScheduledTask] = {}
            for index, record in enumerate(records):
                try:
                    scheduled = ScheduledTask.model_validate(record)
                except (TypeError, ValueError) as exc:
                    logger.error(
                        "Skipping invalid scheduled task record %s in %s: %s",
                        index,
                        self.path,
                        exc,
                    )
                    continue
                if scheduled.status == ScheduledTaskStatus.running:
                    scheduled.status = ScheduledTaskStatus.scheduled
                scheduled.task_id = None
                restored[scheduled.id] = scheduled
            self.tasks = restored

    def _save_locked(self) -> None:
        data = {
            "scheduled_tasks": [
                task.model_dump(mode="json")
                for task in self.tasks.values()
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    async def _run(self) -> None:
        while not self._stop.is_set():
            next_time = self._next_time()
            if next_time is None:
                await self._wait_for_wake()
                continue

            delay = max(
                0,
                (next_time - datetime.now(timezone.utc)).total_seconds(),
            )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

            await self._run_due_tasks()

    def _next_time(self) -> Optional[datetime]:
        with self._lock:
            due = [
                max(
                    task.next_run_at,
                    self._retry_at.get(task.id, task.next_run_at),
                )
                for task in self.tasks.values()
                if task.status == ScheduledTaskStatus.scheduled
            ]
        return min(due) if due else None

    async def _wait_for_wake(self) -> None:
        wake = asyncio.create_task(self._wake.wait())
        stop = asyncio.create_task(self._stop.wait())
        _done, pending = await asyncio.wait(
            {wake, stop},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._wake.clear()

    async def _run_due_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            due = [
                task
                for task in self.tasks.values()
                if (
                    task.status == ScheduledTaskStatus.scheduled
                    and max(
                        task.next_run_at,
                        self._retry_at.get(task.id, task.next_run_at),
                    ) <= now
                )
            ]
        for scheduled in sorted(due, key=lambda task: (task.next_run_at, task.id)):
            if self.manager.has_active():
                with self._lock:
                    self._retry_at[scheduled.id] = now + RETRY_DELAY
                continue
            with self._lock:
                if scheduled.status != ScheduledTaskStatus.scheduled:
                    continue
                scheduled.status = ScheduledTaskStatus.running
                self._save_locked()
            try:
                agent_task = await self.manager.start_task(
                    TaskCreate(
                        goal=scheduled.goal,
                        start_url=scheduled.start_url,
                        profile=scheduled.profile,
                    )
                )
            except RuntimeError as exc:
                if "already active" not in str(exc):
                    if scheduled.recurrence == RecurrenceKind.none:
                        with self._lock:
                            scheduled.status = ScheduledTaskStatus.failed
                            scheduled.error = str(exc)
                            self._save_locked()
                    else:
                        self._reschedule_recurring(scheduled, str(exc))
                    continue
                with self._lock:
                    scheduled.status = ScheduledTaskStatus.scheduled
                    self._retry_at[scheduled.id] = now + RETRY_DELAY
                    self._save_locked()
                continue

            with self._lock:
                scheduled.task_id = agent_task.id
                self._retry_at.pop(scheduled.id, None)
                self._save_locked()
            watcher = asyncio.create_task(
                self._watch(scheduled, agent_task),
                name="handoff-scheduled-" + scheduled.id,
            )
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, scheduled: ScheduledTask, agent_task) -> None:
        while agent_task.status in {
            TaskStatus.running,
            TaskStatus.needs_info,
            TaskStatus.needs_confirmation,
            TaskStatus.needs_human,
        }:
            await asyncio.sleep(POLL_DELAY)
        with self._lock:
            scheduled.last_run_at = scheduled.next_run_at
            if agent_task.status == TaskStatus.completed:
                if scheduled.recurrence == RecurrenceKind.none:
                    scheduled.status = ScheduledTaskStatus.completed
                else:
                    self._reschedule_recurring_locked(scheduled, "")
            else:
                if scheduled.recurrence == RecurrenceKind.none:
                    scheduled.status = ScheduledTaskStatus.failed
                    scheduled.error = agent_task.error
                else:
                    self._reschedule_recurring_locked(scheduled, agent_task.error)
            self._save_locked()

    def _reschedule_recurring(self, scheduled: ScheduledTask, error: str) -> None:
        with self._lock:
            self._reschedule_recurring_locked(scheduled, error)
            self._save_locked()

    def _reschedule_recurring_locked(self, scheduled: ScheduledTask, error: str) -> None:
        next_run_at = _next_occurrence(
            scheduled.next_run_at,
            scheduled.recurrence,
        )
        if next_run_at is None:
            scheduled.status = ScheduledTaskStatus.failed
            scheduled.error = error or "Could not calculate the next occurrence."
            return
        scheduled.next_run_at = next_run_at
        scheduled.status = ScheduledTaskStatus.scheduled
        scheduled.error = error
        self._retry_at.pop(scheduled.id, None)
        self._wake.set()


def _default_schedule_path() -> Path:
    from app.config import settings

    return settings.schedule_file


def _next_occurrence(
    current: datetime,
    recurrence: RecurrenceKind,
) -> Optional[datetime]:
    """Return the next occurrence using the schedule's local timezone."""
    if recurrence == RecurrenceKind.none:
        return None

    local = current.astimezone(current.tzinfo)
    if recurrence == RecurrenceKind.daily:
        target = local.date() + timedelta(days=1)
        return _at_local_date(local, target)
    if recurrence == RecurrenceKind.weekly:
        target = local.date() + timedelta(days=7)
        return _at_local_date(local, target)

    month = local.month + 1
    year = local.year
    if month > 12:
        month = 1
        year += 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


def _at_local_date(current: datetime, target) -> datetime:
    return current.replace(
        year=target.year,
        month=target.month,
        day=target.day,
    )
