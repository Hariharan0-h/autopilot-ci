"""
Deployment agent.

1. Determines strategy: if only test files changed → SKIP,
   if config files changed → ROLLING, otherwise → BLUE_GREEN
2. Calls llm_call(model='deploy') with changed file list and strategy
   to generate a human-readable deploy plan
3. Executes strategy via shell_tools (simulated echo commands in demo mode)
4. Streams log lines and returns DeploymentResult
"""
from __future__ import annotations
import time
from pathlib import Path
from schemas import DeploymentResult, DeployStrategy, SupervisorDecision, AgentStatus
from llm_client import llm_call
from tools.shell_tools import run_command
from rich.console import Console

console = Console()

_CONFIG_EXTENSIONS = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".json"}
_SYSTEM_PROMPT = """You are a deployment engineer in an automated CI pipeline.
Given a list of changed files and a deployment strategy, write a brief 3-4 step
deploy plan that a developer can follow. Be concrete about what happens at each step.
Format as numbered steps. No markdown headers."""


def _select_strategy(changed_files: list[str]) -> DeployStrategy:
    """Select deployment strategy based on which files changed.

    Rules:
      - Only test files changed → SKIP
      - Any config file changed → ROLLING
      - Otherwise → BLUE_GREEN

    Args:
        changed_files: List of relative file paths that changed.

    Returns:
        DeployStrategy enum value.
    """
    if not changed_files:
        return DeployStrategy.SKIP

    non_test = [
        f for f in changed_files
        if not (Path(f).name.startswith("test_") or "/tests/" in f)
    ]
    if not non_test:
        return DeployStrategy.SKIP

    config_changed = any(
        Path(f).suffix in _CONFIG_EXTENSIONS or Path(f).name in {
            "requirements.txt", "Dockerfile", "docker-compose.yml",
            "pyproject.toml", "setup.py", "setup.cfg",
        }
        for f in non_test
    )
    if config_changed:
        return DeployStrategy.ROLLING

    return DeployStrategy.BLUE_GREEN


_DEMO_STEPS: dict[DeployStrategy, list[str]] = {
    DeployStrategy.BLUE_GREEN: [
        "[1/5] Building new container image (green)...",
        "[2/5] Running smoke tests against green environment...",
        "[3/5] Health check passed — green environment is healthy.",
        "[4/5] Switching load balancer traffic: blue → green...",
        "[5/5] Deploy complete. Blue environment standing by for rollback.",
    ],
    DeployStrategy.ROLLING: [
        "[1/4] Updating configuration and dependencies...",
        "[2/4] Rolling restart — pod 1/3 updated.",
        "[3/4] Rolling restart — pod 2/3 updated.",
        "[4/4] Rolling restart — pod 3/3 updated. Deploy complete.",
    ],
    DeployStrategy.SKIP: [
        "[SKIP] Only test/config files changed — no deployment needed.",
    ],
}


async def run_deployment(
    changed_files: list[str],
    supervisor_decision: SupervisorDecision,
    repo_path: str,
    demo_mode: bool = True,
) -> DeploymentResult:
    """Execute deployment strategy for the current push.

    Selects strategy from changed file types, generates a deploy plan via LLM,
    and runs simulated deploy commands (demo mode) or real shell commands.

    Args:
        changed_files: List of relative file paths that changed.
        supervisor_decision: SupervisorDecision from the supervisor agent.
        repo_path: Local path to the git repository.
        demo_mode: If True, run simulated echo commands instead of real deploy.

    Returns:
        DeploymentResult with strategy, log_lines, and duration.
    """
    t0 = time.monotonic()
    strategy = _select_strategy(changed_files)
    log_lines: list[str] = []

    console.print(f"[blue][deployment] Strategy: {strategy.value}[/blue]")
    log_lines.append(f"Strategy selected: {strategy.value}")

    # Get LLM deploy plan
    try:
        plan = await llm_call(
            "deploy",
            _SYSTEM_PROMPT,
            f"Changed files: {changed_files}\n"
            f"Strategy: {strategy.value}\n"
            f"Supervisor summary: {supervisor_decision.summary}",
            temperature=0.3,
            max_tokens=512,
        )
        log_lines.append("Deploy plan:")
        for line in plan.strip().splitlines():
            log_lines.append(f"  {line}")
            console.print(f"[dim]  {line}[/dim]")
    except Exception as e:
        log_lines.append(f"LLM plan unavailable: {e}")

    # Execute strategy
    log_lines.append("Executing:")
    if demo_mode:
        steps = _DEMO_STEPS.get(strategy, _DEMO_STEPS[DeployStrategy.SKIP])
        for step in steps:
            result = await run_command(
                ["echo", step],
                cwd=repo_path,
                timeout_seconds=10,
            )
            log_lines.append(step)
            console.print(f"[green]  {step}[/green]")
    else:
        # Real deploy commands would go here
        result = await run_command(
            ["echo", f"[deploy] {strategy.value} deploy completed"],
            cwd=repo_path,
        )
        log_lines.append(result.stdout.strip())

    return DeploymentResult(
        status=AgentStatus.DONE,
        strategy=strategy,
        log_lines=log_lines,
        duration_seconds=round(time.monotonic() - t0, 2),
    )
