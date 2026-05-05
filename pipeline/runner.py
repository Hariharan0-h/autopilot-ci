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
    SecurityScanResult, PerfAnalysisResult, DockerBuildResult,
)
from tools.git_tools import get_diff, get_changed_files, get_file_content, SCAN_EXTENSIONS
from tools.dotnet_tools import run_dotnet_build
from agents.supervisor import run_supervisor
from agents.code_analyzer import run_code_analyzer
from agents.test_generator import run_test_generator
from agents.security_scanner import run_security_scanner
from agents.perf_analyzer import run_perf_analyzer
from agents.autofix import run_autofix
from agents.deployment import run_deployment
from agents.docker_scanner import run_docker_scanner
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


async def _safe_run_docker(
    repo_path: str,
    run_id: str,
    queue: asyncio.Queue,
) -> DockerBuildResult:
    """Run docker scanner with error isolation."""
    _emit(queue, run_id, "docker_scanner", AgentStatus.RUNNING,
          "Scanning for Dockerfiles and running docker build...")
    try:
        result = await run_docker_scanner(repo_path)
        status_msg = (
            f"Docker build {'succeeded' if result.build_success else 'failed'} — "
            f"{len(result.dockerfiles_found)} Dockerfile(s), "
            f"{len(result.compose_files_found)} compose file(s) found."
        )
        _emit(queue, run_id, "docker_scanner",
              AgentStatus.DONE if result.build_success else AgentStatus.FAILED,
              status_msg)
        return result
    except Exception as e:
        _emit(queue, run_id, "docker_scanner", AgentStatus.FAILED,
              f"Docker scanner error: {e}")
        return DockerBuildResult(status=AgentStatus.FAILED, error=str(e))


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

    # ── Step 1: Get files to scan ─────────────────────────────────────────────

    def _read_all_source_files(repo_root: Path) -> dict[str, str]:
        """Return all git-tracked source files, honouring .gitignore.

        Uses `git ls-files` so untracked/ignored files are never scanned.
        Falls back to rglob if the repo has no git history yet.
        """
        files: dict[str, str] = {}
        try:
            import subprocess
            result = subprocess.run(
                ["git", "ls-files", "--cached"],
                cwd=str(repo_root),
                capture_output=True, text=True, timeout=30,
            )
            tracked = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        except Exception:
            tracked = []

        _SKIP_DIRS = {".git", "node_modules", "__pycache__", "bin", "obj",
                      ".vs", ".idea", "dist", "build", "packages", "migrations"}

        if tracked:
            for rel_path in tracked:
                if not rel_path.endswith(SCAN_EXTENSIONS):
                    continue
                parts = Path(rel_path).parts
                if any(part in _SKIP_DIRS for part in parts):
                    continue
                abs_path = repo_root / rel_path
                try:
                    files[rel_path] = abs_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        else:
            # Fallback: rglob with manual skip list (no git available)
            _SKIP_DIRS = {".git", "node_modules", "__pycache__", "bin", "obj",
                          ".vs", ".idea", "dist", "build", "packages"}
            for f in repo_root.rglob("*"):
                if not f.is_file() or f.suffix not in SCAN_EXTENSIONS:
                    continue
                rel = f.relative_to(repo_root)
                if any(part in _SKIP_DIRS for part in rel.parts):
                    continue
                try:
                    files[str(rel)] = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

        return files

    try:
        repo_root = Path(payload.repo_path)
        changed_files: dict[str, str] = {}

        if getattr(payload, "full_scan", False):
            # Full-repo mode: scan every source file, ignore diff
            changed_files = _read_all_source_files(repo_root)
            _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
                  f"Full-scan mode: {len(changed_files)} source file(s) across whole repo")
        else:
            # Diff mode: only files changed between base and head
            _DIFF_SKIP_DIRS = {"bin", "obj", "packages", ".vs", ".idea",
                               "node_modules", "__pycache__", "dist", "build", "migrations"}
            changed_file_paths = get_changed_files(payload.repo_path, payload.base, payload.head)
            for fp in changed_file_paths:
                if any(part in _DIFF_SKIP_DIRS for part in Path(fp).parts):
                    continue
                content = get_file_content(payload.repo_path, fp, payload.head)
                if content:
                    changed_files[fp] = content

            if not changed_files:
                # No diff found — fall back to full-repo scan so we never skip silently
                changed_files = _read_all_source_files(repo_root)
                _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
                      f"No diff found — falling back to full-repo scan "
                      f"({len(changed_files)} file(s))")
            else:
                _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
                      f"Diff scan: {len(changed_files)} changed file(s) "
                      f"({payload.base} → {payload.head})")

        if not changed_files:
            _emit(event_queue, run_id, "pipeline", AgentStatus.FAILED,
                  "No scannable source files found in repository.")
            run.status = AgentStatus.FAILED
            return run

    except Exception as e:
        _emit(event_queue, run_id, "pipeline", AgentStatus.FAILED, f"Failed to get files: {e}")
        run.status = AgentStatus.FAILED
        return run

    # ── Step 2: Fan out — all 4 agents run concurrently ───────────────────────
    _emit(event_queue, run_id, "pipeline", AgentStatus.RUNNING,
          "Launching 4 analysis agents in parallel (AMD MI300X concurrent inference)...")

    code_result, test_result, security_result, perf_result, docker_result = await asyncio.gather(
        _safe_run_code(changed_files, run_id, event_queue),
        _safe_run_tests(changed_files, payload.repo_path, run_id, event_queue),
        _safe_run_security(changed_files, payload.repo_path, run_id, event_queue),
        _safe_run_perf(changed_files, run_id, event_queue),
        _safe_run_docker(payload.repo_path, run_id, event_queue),
    )

    # Augment code_result with dotnet build diagnostics (catches C# syntax errors
    # and compiler warnings that the LLM may miss when offline)
    try:
        dotnet_findings = run_dotnet_build(payload.repo_path)
        if dotnet_findings:
            code_result.findings = dotnet_findings + code_result.findings
            _emit(event_queue, run_id, "code_analyzer", AgentStatus.DONE,
                  f"dotnet build: {len(dotnet_findings)} compiler diagnostic(s) added")
    except Exception:
        pass

    run.code_result = code_result
    run.test_result = test_result
    run.security_result = security_result
    run.perf_result = perf_result
    run.docker_result = docker_result

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
        if decision.action == PipelineAction.AUTO_FIX and decision.auto_fixable_findings:
            try:
                from agents.autofix import run_autofix
                _emit(event_queue, run_id, "autofix", AgentStatus.RUNNING,
                      "Applying auto-fixes...")
                autofix_result = await run_autofix(
                    decision.auto_fixable_findings, payload.repo_path
                )
                run.autofix_result = autofix_result
                _emit(event_queue, run_id, "autofix", AgentStatus.DONE,
                      f"{autofix_result.fixes_applied} fix(es) applied — "
                      f"branch: {autofix_result.branch_name}")
            except Exception as e:
                _emit(event_queue, run_id, "autofix", AgentStatus.FAILED,
                      f"Auto-fix error: {e}")

        try:
            from agents.deployment import run_deployment
            _emit(event_queue, run_id, "deployment", AgentStatus.RUNNING,
                  "Running deployment...")
            deploy_result = await run_deployment(
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

    # Step 5: Done
    run.status = AgentStatus.DONE
    run.completed_at = datetime.utcnow().isoformat()
    _emit(event_queue, run_id, "pipeline", AgentStatus.DONE,
          f"Pipeline complete. Action taken: {decision.action.value}.")

    # Step 6: Generate PDF report (lazy import — reportlab optional)
    try:
        from agents.report_generator import save_report  # noqa: PLC0415
        reports_dir = Path(__file__).parent.parent / "reports"
        reports_dir.mkdir(exist_ok=True)
        report_path = save_report(run, str(reports_dir))
        _emit(event_queue, run_id, "pipeline", AgentStatus.DONE,
              f"Report saved: reports/{Path(report_path).name}")
    except ImportError:
        _emit(event_queue, run_id, "pipeline", AgentStatus.FAILED,
              "PDF report skipped — install reportlab: pip install reportlab")
    except Exception as e:
        _emit(event_queue, run_id, "pipeline", AgentStatus.FAILED,
              f"Report generation failed: {e}")

    return run
