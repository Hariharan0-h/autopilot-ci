"""
server/webhook.py — FastAPI webhook receiver + HTML dashboard server.

Endpoints:
  GET  /          — serves dashboard/index.html
  POST /webhook   — accepts WebhookPayload, starts pipeline as background task
  POST /trigger   — starts a demo pipeline run (no body required)
  GET  /status/{run_id} — returns PipelineRun status as JSON
  GET  /events/{run_id} — returns list of pipeline events
  GET  /runs      — returns list of all run IDs + statuses
  GET  /gpu       — returns GPU utilization from rocm-smi
  GET  /health    — {"status": "ok", "version": "1.0.0"}
"""
from __future__ import annotations
import asyncio
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from schemas import WebhookPayload, PipelineRun, AgentStatus
from pipeline.runner import run_pipeline

_DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
_DEMO_REPO = Path(__file__).parent.parent / "demo" / "sample_repo"

app = FastAPI(title="AutoPilot CI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static assets (CSS, JS, etc.) from the dashboard directory if needed
if (_DASHBOARD_DIR).exists():
    app.mount("/static", StaticFiles(directory=str(_DASHBOARD_DIR)), name="static")

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


@app.get("/")
async def serve_dashboard() -> FileResponse:
    """Serve the AutoPilot CI HTML dashboard."""
    return FileResponse(str(_DASHBOARD_DIR / "index.html"), media_type="text/html")


@app.post("/trigger", status_code=202)
async def trigger_demo(background_tasks: BackgroundTasks) -> dict:
    """Start a simulated demo pipeline run.

    Emits realistic events with staggered timing so the dashboard animates
    fully without requiring vLLM or a real git repo.

    Returns:
        Dict with run_id and status URL.
    """
    run_id = str(uuid.uuid4())
    ACTIVE_RUNS[run_id] = PipelineRun(
        run_id=run_id,
        repo_path=str(_DEMO_REPO),
        base_commit="HEAD~1",
        head_commit="HEAD",
        status=AgentStatus.RUNNING,
    )
    RUN_EVENTS[run_id] = []
    background_tasks.add_task(_run_demo_simulation, run_id)
    return {"run_id": run_id, "status": "accepted", "status_url": f"/status/{run_id}"}


def _emit(run_id: str, agent: str, status: AgentStatus, message: str) -> None:
    from datetime import datetime
    RUN_EVENTS.setdefault(run_id, []).append({
        "agent": agent,
        "status": status.value,
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
    })


async def _run_demo_simulation(run_id: str) -> None:
    """Emit realistic pipeline events with staggered timing for demo purposes."""
    from schemas import (
        CodeAnalysisResult, TestGenerationResult, SecurityScanResult,
        PerfAnalysisResult, SupervisorDecision, AutoFixResult, DeploymentResult,
        Finding, Severity, PipelineAction, DeployStrategy,
    )

    run = ACTIVE_RUNS[run_id]

    _emit(run_id, "pipeline", AgentStatus.RUNNING,
          "Pipeline started for demo/sample_repo (HEAD~1..HEAD)")
    await asyncio.sleep(0.4)
    _emit(run_id, "pipeline", AgentStatus.DONE,
          "3 .py files changed: app.py, utils.py, queries.py")
    await asyncio.sleep(0.3)

    # Fan-out: 4 agents start in parallel
    for ag in ("code_analyzer", "test_generator", "security_scanner", "perf_analyzer"):
        _emit(run_id, ag, AgentStatus.RUNNING, "Loading model on MI300X HBM3…")
    await asyncio.sleep(0.1)

    # test_generator finishes first
    await asyncio.sleep(1.3)
    run.test_result = TestGenerationResult(tests_written=8, generated_test_files=["tests/test_app.py"])
    _emit(run_id, "test_generator", AgentStatus.DONE, "Generated 8 tests · +12% coverage")

    # code_analyzer
    await asyncio.sleep(0.7)
    run.code_result = CodeAnalysisResult(files_analyzed=3, findings=[
        Finding(file="app.py",    line=42,  severity=Severity.MEDIUM,  category="complexity",  message="Function too complex (cyclomatic=14)", auto_fixable=True),
        Finding(file="utils.py",  line=18,  severity=Severity.LOW,     category="naming",      message="Variable 'x' is not descriptive",     auto_fixable=True),
        Finding(file="queries.py",line=7,   severity=Severity.LOW,     category="dead_code",   message="Unused import 'os'",                   auto_fixable=True),
    ])
    _emit(run_id, "code_analyzer", AgentStatus.DONE, "3 findings: complexity, naming, dead_code")

    # perf_analyzer
    await asyncio.sleep(0.5)
    run.perf_result = PerfAnalysisResult(findings=[
        Finding(file="app.py",    line=124, severity=Severity.HIGH,    category="O(n²)",       message="Nested loop over large dataset",       auto_fixable=False),
        Finding(file="queries.py",line=33,  severity=Severity.HIGH,    category="N+1 query",   message="ORM query inside loop — use select_related", auto_fixable=False),
    ])
    _emit(run_id, "perf_analyzer", AgentStatus.DONE, "2 hotspots: O(n²) loop · N+1 query")

    # security_scanner
    await asyncio.sleep(0.7)
    run.security_result = SecurityScanResult(
        bandit_score=7.0,
        cve_hits=["CVE-2018-18074 (high) — requests<2.20.0", "CVE-2023-32681 (medium) — requests<2.31.0"],
        findings=[
            Finding(file="requirements.txt", line=3, severity=Severity.HIGH, category="CVE", message="requests==2.18.4 has known CVEs", auto_fixable=True),
        ],
    )
    _emit(run_id, "security_scanner", AgentStatus.DONE, "bandit 7.0 · CVE-2018-18074 (requests<2.20)")

    # supervisor aggregates
    await asyncio.sleep(0.4)
    _emit(run_id, "supervisor", AgentStatus.RUNNING, "Aggregating · weighing severity vs auto-fixability")
    await asyncio.sleep(1.1)
    run.supervisor_decision = SupervisorDecision(
        action=PipelineAction.AUTO_FIX,
        summary="2 high-severity CVEs and 3 low-severity findings are auto-fixable. Applying patches and opening PR — deployment held for review.",
        total_findings=6, critical_count=0, high_count=2, medium_count=1, low_count=3,
        confidence=0.86,
        auto_fixable_findings=[run.security_result.findings[0], run.code_result.findings[0]],
    )
    _emit(run_id, "supervisor", AgentStatus.DONE, "decision: AUTO_FIX (conf 0.86)")

    # autofix
    _emit(run_id, "autofix", AgentStatus.RUNNING, "str.replace patches · creating branch")
    await asyncio.sleep(1.2)
    run.autofix_result = AutoFixResult(
        fixes_applied=4,
        branch_name="autopilot/fix-cve-perf-3a8",
        pr_url="https://github.com/org/repo/pull/142",
        pr_body="## AutoPilot CI — Auto-Fix PR\n\nPatched 2 CVEs and 2 code findings.",
    )
    _emit(run_id, "autofix", AgentStatus.DONE, "4 patches applied · PR #142 opened")

    # deployment — held
    run.deployment_result = DeploymentResult(strategy=DeployStrategy.SKIP, log_lines=["Held — awaiting PR review"])
    _emit(run_id, "deployment", AgentStatus.WAITING, "Held — DEPLOY blocked until PR merged")

    run.status = AgentStatus.DONE
    from datetime import datetime
    run.completed_at = datetime.utcnow().isoformat()
    _emit(run_id, "pipeline", AgentStatus.DONE, "Pipeline complete · AUTO_FIX applied")


@app.get("/gpu")
async def get_gpu_util() -> dict:
    """Return AMD GPU utilization via rocm-smi, or a mock value if unavailable.

    Returns:
        Dict with util string (e.g. {"util": "42%"}).
    """
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "GPU use" in line or ("%" in line and line.strip()):
                    import re
                    m = re.search(r"(\d+)\s*%", line)
                    if m:
                        return {"util": m.group(1) + "%", "source": "rocm-smi"}
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return {"util": "0%", "source": "mock"}


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
