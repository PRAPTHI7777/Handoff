import unittest

from app.agent import AgentTask, TaskManager
from app.schemas import ResumeRequest, TaskCreate, TaskStatus


class TaskManagerResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = TaskManager()
        self.task = AgentTask(TaskCreate(goal="Complete the local verification step"))
        self.task.status = TaskStatus.needs_human
        self.manager.tasks[self.task.id] = self.task

    def assert_resume_rejected_without_releasing_task(self, payload: ResumeRequest) -> None:
        with self.assertRaisesRegex(RuntimeError, "Human handoff must be confirmed"):
            self.manager.resume(self.task.id, payload)

        self.assertEqual(self.task.status, TaskStatus.needs_human)
        self.assertFalse(self.task.resume_event.is_set())
        self.assertIsNone(self.task.resume_payload)

    def test_human_handoff_rejects_omitted_confirmation(self) -> None:
        self.assert_resume_rejected_without_releasing_task(ResumeRequest())

    def test_human_handoff_rejects_false_confirmation(self) -> None:
        self.assert_resume_rejected_without_releasing_task(
            ResumeRequest(human_done=False)
        )

    def test_human_handoff_resumes_after_true_confirmation(self) -> None:
        payload = ResumeRequest(human_done=True)

        resumed = self.manager.resume(self.task.id, payload)

        self.assertIs(resumed, self.task)
        self.assertTrue(self.task.resume_event.is_set())
        self.assertIs(self.task.resume_payload, payload)
        self.assertEqual(self.task.status, TaskStatus.needs_human)
