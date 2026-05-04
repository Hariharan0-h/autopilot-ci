"""
server/webhook.py — FastAPI webhook receiver.

Endpoints:
  POST /webhook  — accepts WebhookPayload, starts pipeline as background task
  GET  /status/{run_id} — returns PipelineRun status as JSON
  GET  /runs     — returns list of all run IDs + statuses
  GET  /health   — {"status": "ok", "version": "1.0.0"}

A module-level asyncio.Queue (EVENT_QUEUE) is shared with dashboard/app.py.
Active runs are tracked in a module-level dict ACTIVE_RUNS: dict[str, PipelineRun].
"""
from __future__ import annotations
import asyncio
import uuid

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from schemas import WebhookPayload, PipelineRun, AgentStatus
from pipeline.runner import run_pipeline

app = FastAPI(title="AutoPilot CI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EVENT_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=1000)
ACTIVE_RUNS: dict[str, PipelineRun] = {}
RUN_EVENTS: dict[str, list[dict]] = {}  # run_id → list of event dicts


async def _drain_events(run_id: str) -> None:
    """Drain EVENT_QUEUE into RUN_EVENTS[run_id] continuously until run completes."""
    while True:
        try:
            event = EVENT_QUEUE.get_nowait()
            if event.run_id == run_id:
                RUN_EVENTS.setdefault(run_id, []).append({
                    "agent": event.agent,
                    "status": event.status.value,
                    "message": event.message,
                    "timestamp": event.timestamp,
                })
            else:
                # Put back events for other runs
                await EVENT_QUEUE.put(event)
        except asyncio.QueueEmpty:
            if run_id in ACTIVE_RUNS and ACTIVE_RUNS[run_id].status in (
                AgentStatus.DONE, AgentStatus.FAILED
            ):
                break
            await asyncio.sleep(0.1)


async def _execute_pipeline(payload: WebhookPayload, run_id: str) -> None:
    """Background task: run the full pipeline and update ACTIVE_RUNS.

    Args:
        payload: WebhookPayload with repo info and commit SHAs.
        run_id: Unique ID for this pipeline run.
    """
    RUN_EVENTS[run_id] = []
    try:
        drain_task = asyncio.create_task(_drain_events(run_id))
        completed_run = await run_pipeline(payload, run_id, EVENT_QUEUE)
        ACTIVE_RUNS[run_id] = completed_run
        await asyncio.sleep(0.3)  # Let drain catch final events
        drain_task.cancel()
    except Exception as e:
        if run_id in ACTIVE_RUNS:
            ACTIVE_RUNS[run_id].status = AgentStatus.FAILED


@app.post("/webhook", status_code=202)
async def receive_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
) -> dict:
    """Accept a webhook payload and start the CI pipeline as a background task.

    Args:
        payload: WebhookPayload with repo_path, base, head, branch.
        background_tasks: FastAPI background task runner.

    Returns:
        Dict with run_id and status URL.
    """
    run_id = str(uuid.uuid4())

    # Pre-register run so status endpoint works immediately
    ACTIVE_RUNS[run_id] = PipelineRun(
        run_id=run_id,
        repo_path=payload.repo_path,
        base_commit=payload.base,
        head_commit=payload.head,
        status=AgentStatus.RUNNING,
    )

    background_tasks.add_task(_execute_pipeline, payload, run_id)

    return {
        "run_id": run_id,
        "status": "accepted",
        "status_url": f"/status/{run_id}",
    }


@app.get("/status/{run_id}")
async def get_run_status(run_id: str) -> dict:
    """Return the current status and results of a pipeline run.

    Args:
        run_id: Pipeline run ID returned by POST /webhook.

    Returns:
        Full PipelineRun as JSON dict.

    Raises:
        HTTPException 404: If run_id is not found.
    """
    if run_id not in ACTIVE_RUNS:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return ACTIVE_RUNS[run_id].model_dump()


@app.get("/events/{run_id}")
async def get_run_events(run_id: str) -> list[dict]:
    """Return all pipeline events emitted for a run (agent messages, status changes).

    Args:
        run_id: Pipeline run ID.

    Returns:
        List of event dicts with agent, status, message, timestamp.
    """
    return RUN_EVENTS.get(run_id, [])


@app.get("/runs")
async def list_runs() -> list[dict]:
    """Return list of all run IDs, statuses, and start times.

    Returns:
        List of dicts with run_id, status, started_at, completed_at.
    """
    return [
        {
            "run_id": run.run_id,
            "status": run.status.value,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "repo_path": run.repo_path,
        }
        for run in ACTIVE_RUNS.values()
    ]


@app.get("/health")
async def health() -> dict:
    """Return service health status.

    Returns:
        Dict with status and version.
    """
    return {"status": "ok", "version": "1.0.0", "active_runs": len(ACTIVE_RUNS)}
