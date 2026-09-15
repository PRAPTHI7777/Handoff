import asyncio
import unittest
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path

from app.scheduler import Scheduler, _next_occurrence
from app.schemas import (
    RecurrenceKind,
    ScheduledTask,
    ScheduledTaskStatus,
    ScheduledTaskCreate,
    TaskStatus,
)


class FakeAgentTask:
    def __init__(self, status: TaskStatus, error: str = "", task_id: str = "agent-id") -> None:
        self.id = task_id
        self.status = status
        self.error = error


class FakeManager:
    def __init__(self, agent_status: TaskStatus = TaskStatus.completed, error: str = "") -> None:
        self.agent_status = agent_status
        self.error = error
        self.started = []

    def has_active(self) -> bool:
        return False

    async def start_task(self, spec) -> FakeAgentTask:
        self.started.append(spec)
        return FakeAgentTask(self.agent_status, self.error)


def scheduled_task(
    run_at: datetime,
    recurrence: RecurrenceKind = RecurrenceKind.none,
) -> ScheduledTask:
    return ScheduledTask(
        id="scheduled-id",
        goal="test",
        run_at=run_at,
        recurrence=recurrence,
        next_run_at=run_at,
        status=ScheduledTaskStatus.running,
    )


class SchedulerRecurrenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scheduler = Scheduler(manager=None)  # type: ignore[arg-type]
        self.run_at = datetime(2026, 1, 31, 22, 30, tzinfo=timezone.utc)

    def test_none_has_no_next_occurrence(self) -> None:
        self.assertIsNone(_next_occurrence(self.run_at, RecurrenceKind.none))

    def test_daily_recurrence(self) -> None:
        next_run = _next_occurrence(self.run_at, RecurrenceKind.daily)
        self.assertEqual(next_run, datetime(2026, 2, 1, 22, 30, tzinfo=timezone.utc))

    def test_weekly_recurrence(self) -> None:
        next_run = _next_occurrence(self.run_at, RecurrenceKind.weekly)
        self.assertEqual(next_run, datetime(2026, 2, 7, 22, 30, tzinfo=timezone.utc))

    def test_monthly_recurrence_clamps_january_31(self) -> None:
        next_run = _next_occurrence(self.run_at, RecurrenceKind.monthly)
        self.assertEqual(next_run, datetime(2026, 2, 28, 22, 30, tzinfo=timezone.utc))
        leap_year = datetime(2028, 1, 31, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(
            _next_occurrence(leap_year, RecurrenceKind.monthly),
            datetime(2028, 2, 29, 22, 30, tzinfo=timezone.utc),
        )

    async def test_none_completion_remains_completed(self) -> None:
        scheduled = scheduled_task(self.run_at)
        await self.scheduler._watch(
            scheduled,
            FakeAgentTask(TaskStatus.completed),
        )
        self.assertEqual(scheduled.status, ScheduledTaskStatus.completed)
        self.assertEqual(scheduled.last_run_at, self.run_at)

    async def test_recurring_completion_returns_to_scheduled(self) -> None:
        scheduled = scheduled_task(self.run_at, RecurrenceKind.daily)
        await self.scheduler._watch(
            scheduled,
            FakeAgentTask(TaskStatus.completed),
        )
        self.assertEqual(scheduled.status, ScheduledTaskStatus.scheduled)
        self.assertEqual(scheduled.last_run_at, self.run_at)
        self.assertEqual(scheduled.next_run_at, datetime(2026, 2, 1, 22, 30, tzinfo=timezone.utc))

    async def test_recurring_failure_schedules_next_occurrence(self) -> None:
        scheduled = scheduled_task(self.run_at, RecurrenceKind.weekly)
        await self.scheduler._watch(
            scheduled,
            FakeAgentTask(TaskStatus.failed, "browser failed"),
        )
        self.assertEqual(scheduled.status, ScheduledTaskStatus.scheduled)
        self.assertEqual(scheduled.error, "browser failed")
        self.assertEqual(scheduled.last_run_at, self.run_at)
        self.assertEqual(scheduled.next_run_at, datetime(2026, 2, 7, 22, 30, tzinfo=timezone.utc))


class SchedulerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "scheduled_tasks.json"
        self.run_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_spec(self, recurrence=RecurrenceKind.none) -> ScheduledTaskCreate:
        return ScheduledTaskCreate(
            goal="test",
            run_at=self.run_at,
            recurrence=recurrence,
        )

    def test_save_load_round_trip_preserves_recurrence_and_timezone(self) -> None:
        scheduler = Scheduler(FakeManager(), self.path)
        created = scheduler.schedule(self.make_spec(RecurrenceKind.monthly))
        created.last_run_at = datetime(2025, 12, 31, 22, 30, tzinfo=timezone.utc)
        scheduler._save()

        restored = Scheduler(FakeManager(), self.path)
        restored.load()
        loaded = restored.tasks[created.id]
        self.assertEqual(loaded.model_dump(), created.model_dump())
        self.assertEqual(loaded.recurrence, RecurrenceKind.monthly)
        self.assertEqual(loaded.next_run_at.tzinfo, timezone.utc)

    def test_terminal_and_cancelled_statuses_persist(self) -> None:
        scheduler = Scheduler(FakeManager(), self.path)
        for status, task_id in (
            (ScheduledTaskStatus.cancelled, None),
            (ScheduledTaskStatus.completed, "done"),
            (ScheduledTaskStatus.failed, "failed"),
        ):
            task = scheduled_task(self.run_at)
            task.id = status.value
            task.status = status
            task.task_id = task_id
            scheduler.tasks[task.id] = task
        scheduler._save()

        restored = Scheduler(FakeManager(), self.path)
        restored.load()
        self.assertEqual(
            {task.status for task in restored.tasks.values()},
            {
                ScheduledTaskStatus.cancelled,
                ScheduledTaskStatus.completed,
                ScheduledTaskStatus.failed,
            },
        )

    async def test_recurring_failure_is_saved_and_rescheduled(self) -> None:
        scheduler = Scheduler(FakeManager(), self.path)
        task = scheduled_task(self.run_at, RecurrenceKind.daily)
        scheduler.tasks[task.id] = task
        await scheduler._watch(task, FakeAgentTask(TaskStatus.failed, "failed"))

        restored = Scheduler(FakeManager(), self.path)
        restored.load()
        loaded = restored.tasks[task.id]
        self.assertEqual(loaded.status, ScheduledTaskStatus.scheduled)
        self.assertEqual(loaded.error, "failed")
        self.assertEqual(loaded.next_run_at, self.run_at + timedelta(days=1))

    async def test_past_one_time_schedule_runs_once(self) -> None:
        manager = FakeManager()
        scheduler = Scheduler(manager, self.path)
        task = scheduled_task(datetime(2020, 1, 1, tzinfo=timezone.utc))
        task.status = ScheduledTaskStatus.scheduled
        scheduler.tasks[task.id] = task
        await scheduler._run_due_tasks()
        await asyncio.sleep(0)
        self.assertEqual(len(manager.started), 1)
        self.assertEqual(scheduler.tasks[task.id].status, ScheduledTaskStatus.completed)

    async def test_past_recurring_schedule_does_not_replay_missed_occurrences(self) -> None:
        manager = FakeManager()
        scheduler = Scheduler(manager, self.path)
        task = scheduled_task(
            datetime(2020, 1, 31, 22, 30, tzinfo=timezone.utc),
            RecurrenceKind.monthly,
        )
        task.status = ScheduledTaskStatus.scheduled
        scheduler.tasks[task.id] = task
        await scheduler._run_due_tasks()
        await asyncio.sleep(0)
        self.assertEqual(len(manager.started), 1)
        self.assertEqual(scheduler.tasks[task.id].next_run_at, datetime(2020, 2, 29, 22, 30, tzinfo=timezone.utc))

    def test_stale_running_schedule_is_recovered(self) -> None:
        scheduler = Scheduler(FakeManager(), self.path)
        task = scheduled_task(self.run_at)
        task.status = ScheduledTaskStatus.running
        task.task_id = "stale"
        scheduler.tasks[task.id] = task
        scheduler._save()

        restored = Scheduler(FakeManager(), self.path)
        restored.load()
        loaded = restored.tasks[task.id]
        self.assertEqual(loaded.status, ScheduledTaskStatus.scheduled)
        self.assertIsNone(loaded.task_id)

    def test_malformed_json_is_not_overwritten(self) -> None:
        self.path.write_text("{not-json", encoding="utf-8")
        scheduler = Scheduler(FakeManager(), self.path)
        scheduler.load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not-json")
        self.assertEqual(scheduler.tasks, {})

    def test_malformed_records_are_skipped(self) -> None:
        self.path.write_text(
            json.dumps({"scheduled_tasks": [{"id": "bad"}, scheduled_task(self.run_at).model_dump(mode="json")]}),
            encoding="utf-8",
        )
        scheduler = Scheduler(FakeManager(), self.path)
        scheduler.load()
        self.assertEqual(len(scheduler.tasks), 1)

    def test_atomic_write_leaves_valid_file_without_temporary_file(self) -> None:
        scheduler = Scheduler(FakeManager(), self.path)
        scheduler.schedule(self.make_spec())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("scheduled_tasks", data)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
