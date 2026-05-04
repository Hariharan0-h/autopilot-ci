"""
pipeline/runner.py — Orchestrates one full AutoPilot CI run.

Flow:
  1. Get diff (changed files + their source)
  2. Fan out: run all 4 analysis agents CONCURRENTLY via asyncio.gather
  3. Supervisor aggregates results → SupervisorDecision
  4. Branch on action:
       AUTO_FIX  → autofix agent → deployment agent
       ESCALATE  → emit escalation event, skip autofix
       DEPLOY    → deployment agent only
  5. Emit PipelineEvent to event_queue at every meaningful step

Every step is wrapped in try/except — a failing agent emits an error event
and the pipeline continues with the remaining agents' results.
"""

from __future__ import annotations
import asyncio
from datetime import datetime
from pathlib import Path

from schemas import (
    PipelineRun, PipelineEvent, AgentStatus, PipelineAction,
    WebhookPayload, CodeAnalysisResult, TestGenerationResult,
    SecurityScanResult, PerfAnalysisResult,
)
from tools.git_tools import get_diff, get_changed_files, get_file_content
from agents.supervisor import run_supervisor
from agents.code_analyzer import run_code_analyzer
from agents.test_generator import run_test_generator
from agents.security_scanner import run_security_scanner
from agents.perf_analyzer import run_perf_analyzer
from agents.autofix import run_autofix
from agents.deployment import run_deployment
from rich.console import Console

console = Console()


def _emit(
    queue: asyncio.Queue,
    run_id: str,
    agent: str,
    status: AgentStatus,
    message: str,
    payload: dict | None = None,
) -> None:
    """Put a PipelineEvent onto the queue without blocking.

    Args:
        queue: Asyncio queue for pipeline events.
        run_id: Unique pipeline run ID.
        agent: Agent name emitting the event.
        status: Current agent status.
        message: Human-readable event message.
        payload: Optional structured data dict.
    """
    event = PipelineEvent(
        run_id=run_id,
        agent=agent,
        status=status,
        message=message,
        payload=payload,
    )
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


async def _safe_run_code(
    changed_files: dict[str, str],
    run_id: str,
    queue: asyncio.Queue,
) -> CodeAnalysisResult:
    """Run code analyzer with error isolation.

    Args:
        changed_files: Dict mapping filepath to source code.
        run_id: Pipeline run ID for events.
        queue: Event queue.

    Returns:
        CodeAnalysisResult (FAILED status if exception occurs).
    """
    _emit(queue, run_id, "code_analyzer", AgentStatus.RUNNING, "Analyzing code quality...")
    try:
        result = await run_code_analyzer(changed_files)
        _emit(
            queue, run_id, "code_analyzer", AgentStatus.DONE,
            f"Code analysis complete — {len(result.findings)} finding(s).",
            payload={"findings_count": len(result.findings)},
        )
        return result
    except Exception as e:
        _emit(queue, run_id, "code_analyzer", AgentStatus.FAILED, f"Code analyzer error: {e}")
        return CodeAnalysisResult(status=AgentStatus.FAILED, error=str(e))


async def _safe_run_tests(
    changed_files: dict[str, str],
    repo_path: str,
    run_id: str,
    queue: asyncio.Queue,
) -> TestGenerationResult:
    """Run test generator with error isolation.

    Args:
        changed_files: Dict mapping filepath to source code.
        repo_path: Repository root path.
        run_id: Pipeline run ID for events.
        queue: Event queue.

    Returns:
        TestGenerationResult (FAILED status if exception occurs).
    """
    _emit(queue, run_id, "test_generator", AgentStatus.RUNNING, "Finding untested functions...")
    try:
        result = await run_test_generator(changed_files, repo_path)
        _emit(
            queue, run_id, "test_generator", AgentStatus.DONE,
            f"Test generation complete — {result.tests_written} test(s) written.",
            payload={"tests_written": result.tests_written},
        )
        return result
    except Exception as e:
        _emit(queue, run_id, "test_generator", AgentStatus.FAILED, f"Test generator error: {e}")
        return TestGenerationResult(status=AgentStatus.FAILED, error=str(e))


async def _safe_run_security(
    changed_files: dict[str, str],
    repo_path: str,
    run_id: str,
    queue: asyncio.Queue,
) -> SecurityScanResult:
    """Run security scanner with error isolation.

    Args:
        changed_files: Dict mapping filepath to source code.
        repo_path: Repository root path.
        run_id: Pipeline run ID for events.
        queue: Event queue.

    Returns:
        SecurityScanResult (FAILED status if exception occurs).
    """
    _emit(queue, run_id, "security_scanner", AgentStatus.RUNNING, "Running security scan...")
    try:
        result = await run_security_scanner(changed_files, repo_path)
        _emit(
            queue, run_id, "security_scanner", AgentStatus.DONE,
            f"Security scan complete — bandit_score={result.bandit_score}, "
            f"{len(result.cve_hits)} CVE hit(s).",
            payload={"bandit_score": result.bandit_score, "cve_count": len(result.cve_hits)},
        )
        return result
    except Exception as e:
        _emit(queue, run_id, "security_scanner", AgentStatus.FAILED, f"Security scanner error: {e}")
        return SecurityScanResult(status=AgentStatus.FAILED, error=str(e))


async def _safe_run_perf(
    changed_files: dict[str, str],
    run_id: str,
    queue: asyncio.Queue,
) -> PerfAnalysisResult:
    """Run performance analyzer with error isolation.

    Args:
        changed_files: Dict mapping filepath to source code.
        run_id: Pipeline run ID for events.
        queue: Event queue.

    Returns:
        PerfAnalysisResult (FAILED status if exception occurs).
    """
    _emit(queue, run_id, "perf_analyzer", AgentStatus.RUNNING, "Analyzing performance...")
    try:
        result = await run_perf_analyzer(changed_files)
        _emit(
            queue, run_id, "perf_analyzer", AgentStatus.DONE,
            f"Performance analysis complete — {len(result.findings)} finding(s).",
            payload={"findings_count": len(result.findings)},
        )
        return result
    except Exception as e:
        _emit(queue, run_id, "perf_analyzer", AgentStatus.FAILED, f"Perf analyzer error: {e}")
        return PerfAnalysisResult(status=AgentStatus.FAILED, error=str(e))


async def run_pipeline(
    payload: WebhookPayload,
    run_id: str,
    event_queue: asyncio.Queue,
) -> PipelineRun:
    """Main pipeline entry point. Returns completed PipelineRun.

    Runs 4 analysis agents concurrently, feeds results to supervisor,
    then branches to auto-fix or escalation based on supervisor decision.
    All events are emitted to event_queue throughout execution.

    Args:
        payload: WebhookPayload with repo_path, base, head commits.
        run_id: Unique ID for this pipeline run.
        event_queue: Asyncio queue for PipelineEvent objects.

    Returns:
        Completed PipelineRun with all agent results attached.
    """
    run = PipelineRun(
        run_id=run_id,
        repo_path=payload.repo_path,
        base_commit=payload.base,
        head_commit=payload.head,
        status=AgentStatus.RUNNING,
    )

    _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
          f"Pipeline started for {payload.repo_path} ({payload.base[:8]}..{payload.head[:8]})")

    # ── Step 1: Get changed files ──────────────────────────────────────────────
    try:
        changed_file_paths = get_changed_files(payload.repo_path, payload.base, payload.head)
        changed_files: dict[str, str] = {}

        for fp in changed_file_paths:
            content = get_file_content(payload.repo_path, fp, payload.head)
            if content:
                changed_files[fp] = content

        if not changed_files:
            # Fallback: read all Python files from repo root
            repo_root = Path(payload.repo_path)
            for py_file in repo_root.rglob("*.py"):
                rel = str(py_file.relative_to(repo_root))
                if "tests" not in rel and "__pycache__" not in rel:
                    changed_files[rel] = py_file.read_text(encoding="utf-8", errors="replace")

        _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
              f"Found {len(changed_files)} changed Python file(s): {list(changed_files.keys())}")
    except Exception as e:
        _emit(event_queue, run_id, "pipeline", AgentStatus.FAILED, f"Failed to get diff: {e}")
        run.status = AgentStatus.FAILED
        return run

    # ── Step 2: Fan out — all 4 agents run concurrently ───────────────────────
    _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
          "Launching 4 analysis agents in parallel (AMD MI300X concurrent inference)...")

    code_result, test_result, security_result, perf_result = await asyncio.gather(
        _safe_run_code(changed_files, run_id, event_queue),
        _safe_run_tests(changed_files, payload.repo_path, run_id, event_queue),
        _safe_run_security(changed_files, payload.repo_path, run_id, event_queue),
        _safe_run_perf(changed_files, run_id, event_queue),
    )

    run.code_result = code_result
    run.test_result = test_result
    run.security_result = security_result
    run.perf_result = perf_result

    # ── Step 3: Supervisor decision ────────────────────────────────────────────
    _emit(event_queue, run_id, "supervisor", AgentStatus.RUNNING,
          "Supervisor aggregating results...")
    try:
        decision = await run_supervisor(code_result, test_result, security_result, perf_result)
        run.supervisor_decision = decision
        _emit(
            event_queue, run_id, "supervisor", AgentStatus.DONE,
            f"Decision: {decision.action.value} — {decision.summary}",
            payload={"action": decision.action.value, "total_findings": decision.total_findings},
        )
    except Exception as e:
        _emit(event_queue, run_id, "supervisor", AgentStatus.FAILED, f"Supervisor error: {e}")
        run.status = AgentStatus.FAILED
        run.completed_at = datetime.utcnow().isoformat()
        return run

    # ── Step 4: Branch on action ───────────────────────────────────────────────
    if decision.action == PipelineAction.ESCALATE:
        _emit(
            event_queue, run_id, "pipeline", AgentStatus.DONE,
            f"ESCALATE: {decision.critical_count} critical finding(s) require human review. "
            "Skipping auto-fix.",
        )
        run.status = AgentStatus.DONE
        run.completed_at = datetime.utcnow().isoformat()
        return run

    if decision.action in (PipelineAction.AUTO_FIX, PipelineAction.DEPLOY):
        # Auto-fix first if needed
        if decision.action == PipelineAction.AUTO_FIX and decision.auto_fixable_findings:
            _emit(event_queue, run_id, "autofix", AgentStatus.RUNNING,
                  f"Auto-fixing {len(decision.auto_fixable_findings)} finding(s)...")
            try:
                fix_result = await run_autofix(
                    decision,
                    payload.repo_path,
                    payload.repo_url,
                )
                run.autofix_result = fix_result
                _emit(
                    event_queue, run_id, "autofix", AgentStatus.DONE,
                    f"Auto-fix complete — {fix_result.fixes_applied} fix(es) applied. "
                    + (f"PR: {fix_result.pr_url}" if fix_result.pr_url else "Branch pushed."),
                    payload={"fixes_applied": fix_result.fixes_applied, "pr_url": fix_result.pr_url},
                )
            except Exception as e:
                _emit(event_queue, run_id, "autofix", AgentStatus.FAILED, f"Auto-fix error: {e}")

        # Deployment
        _emit(event_queue, run_id, "deployment", AgentStatus.RUNNING, "Running deployment...")
        try:
            deploy_result = await run_deployment(
                changed_file_paths,
                decision,
                payload.repo_path,
                demo_mode=True,
            )
            run.deployment_result = deploy_result
            _emit(
                event_queue, run_id, "deployment", AgentStatus.DONE,
                f"Deploy complete — strategy: {deploy_result.strategy.value}",
                payload={"strategy": deploy_result.strategy.value},
            )
        except Exception as e:
            _emit(event_queue, run_id, "deployment", AgentStatus.FAILED, f"Deployment error: {e}")

    # ── Step 5: Done ───────────────────────────────────────────────────────────
    run.status = AgentStatus.DONE
    run.completed_at = datetime.utcnow().isoformat()
    _emit(event_queue, run_id, "pipeline", AgentStatus.DONE,
          f"Pipeline complete. Action taken: {decision.action.value}.")
    return run
