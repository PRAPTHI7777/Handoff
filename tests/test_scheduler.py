import unittest
from datetime import datetime, timezone

from app.scheduler import Scheduler, _next_occurrence
from app.schemas import (
    RecurrenceKind,
    ScheduledTask,
    ScheduledTaskStatus,
    TaskStatus,
)


class FakeAgentTask:
    def __init__(self, status: TaskStatus, error: str = "") -> None:
        self.status = status
        self.error = error


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


if __name__ == "__main__":
    unittest.main()
