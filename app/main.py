import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from pydantic import BaseModel

from app.agent import manager
from app.scheduler import Scheduler
from app.schemas import (
    ResumeRequest,
    ScheduledTask,
    ScheduledTaskCreate,
    TaskCreate,
    TaskStatus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    playwright = await async_playwright().start()
    scheduler = Scheduler(manager)
    try:
        manager.attach(playwright)
        await scheduler.start()
        app.state.scheduler = scheduler
        yield
    finally:
        await scheduler.stop()
        await playwright.stop()


app = FastAPI(title="Handoff", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class TaskView(BaseModel):
    id: str
    status: TaskStatus
    goal: str
    step: int
    backend: str
    summary: str
    error: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    from app.config import settings

    return {
        "ok": True,
        "groq_configured": bool(settings.groq_api_key),
        "anakin_configured": bool(settings.anakin_api_key),
        "browser_mode": settings.browser_mode,
    }


@app.post("/tasks", response_model=TaskView)
async def create_task(spec: TaskCreate) -> TaskView:
    try:
        task = await manager.start_task(spec)
    except RuntimeError as exc:
        message = str(exc)
        if "already active" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        if "GROQ_API_KEY" in message:
            raise HTTPException(status_code=400, detail=message) from exc
        raise HTTPException(status_code=500, detail=message) from exc
    return _view(task)


@app.post("/scheduled-tasks", response_model=ScheduledTask)
async def create_scheduled_task(spec: ScheduledTaskCreate) -> ScheduledTask:
    return app.state.scheduler.schedule(spec)


@app.get("/scheduled-tasks", response_model=list[ScheduledTask])
async def list_scheduled_tasks() -> list[ScheduledTask]:
    return app.state.scheduler.list_tasks()


@app.delete("/scheduled-tasks/{scheduled_id}", response_model=ScheduledTask)
async def delete_scheduled_task(scheduled_id: str) -> ScheduledTask:
    try:
        return app.state.scheduler.delete(scheduled_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown scheduled task") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(task_id: str) -> TaskView:
    try:
        task = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown task") from exc
    return _view(task)


@app.post("/tasks/{task_id}/resume", response_model=TaskView)
async def resume_task(task_id: str, payload: ResumeRequest) -> TaskView:
    try:
        task = manager.resume(task_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown task") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(task)


@app.get("/tasks/{task_id}/screenshot")
async def screenshot(task_id: str) -> StreamingResponse:
    try:
        task = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown task") from exc
    if not task.screenshot:
        raise HTTPException(status_code=404, detail="No screenshot yet")
    return StreamingResponse(iter([task.screenshot]), media_type="image/png")


@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str) -> StreamingResponse:
    try:
        task = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown task") from exc

    async def gen():
        import json

        index = 0
        while True:
            while index < len(task.events):
                yield f"data: {json.dumps(task.events[index])}\n\n"
                index += 1
            if task.status in {TaskStatus.completed, TaskStatus.failed} and index >= len(task.events):
                break
            await task.queue.get()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _view(task) -> TaskView:
    return TaskView(
        id=task.id,
        status=task.status,
        goal=task.goal,
        step=task.step,
        backend=task.backend,
        summary=task.summary,
        error=task.error,
    )
