"""
Auto-fix agent.

1. For each auto_fixable finding in supervisor_decision.auto_fixable_findings:
   a. Calls llm_call(model='coder') with original snippet + suggestion,
      asking for the corrected code block only
   b. Applies the fix using str.replace on the source file
2. Uses git_tools to: create branch, commit each fix, push
3. Builds a detailed PR body (markdown table of fixes + before/after snippets)
4. Calls git_tools.open_pull_request()
5. Returns AutoFixResult
"""
from __future__ import annotations
import os
import time
from datetime import datetime
from pathlib import Path
from schemas import AutoFixResult, SupervisorDecision, AgentStatus
from llm_client import llm_call
from tools import git_tools
from rich.console import Console

console = Console()

_SYSTEM_PROMPT = """You are an automated code fixer in a CI pipeline.
Given an original code snippet and a fix suggestion, output ONLY the corrected
replacement code. Do not include any explanation, markdown fences, or surrounding
context — just the fixed code lines that should replace the original snippet.
Preserve the original indentation."""


async def run_autofix(
    supervisor_decision: SupervisorDecision,
    repo_path: str,
    repo_url: str = "",
) -> AutoFixResult:
    """Apply auto-fixes for all auto_fixable findings and open a PR.

    Creates a new branch, applies each fix via LLM-guided str.replace,
    commits each fix, pushes the branch, and opens a GitHub PR.

    Args:
        supervisor_decision: SupervisorDecision containing auto_fixable_findings.
        repo_path: Local path to the git repository.
        repo_url: GitHub repo URL for PR creation (optional).

    Returns:
        AutoFixResult with branch name, commit hashes, and PR URL.
    """
    t0 = time.monotonic()
    fixable = supervisor_decision.auto_fixable_findings

    if not fixable:
        return AutoFixResult(
            status=AgentStatus.DONE,
            fixes_applied=0,
            duration_seconds=round(time.monotonic() - t0, 2),
        )

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    branch_name = f"autopilot/fix-{timestamp}"
    commit_hashes: list[str] = []
    pr_rows: list[str] = []

    try:
        git_tools.create_branch(repo_path, branch_name)
    except Exception as e:
        console.print(f"[yellow][autofix] Could not create branch: {e}[/yellow]")
        return AutoFixResult(
            status=AgentStatus.FAILED,
            error=str(e),
            duration_seconds=round(time.monotonic() - t0, 2),
        )

    fixes_applied = 0
    for finding in fixable:
        file_path = Path(repo_path) / finding.file
        if not file_path.exists():
            console.print(f"[yellow][autofix] File not found: {file_path}[/yellow]")
            continue

        original_source = file_path.read_text(encoding="utf-8")
        if not finding.snippet or finding.snippet not in original_source:
            console.print(f"[yellow][autofix] Snippet not found in {finding.file}, skipping[/yellow]")
            continue

        user_prompt = (
            f"File: {finding.file}\n"
            f"Issue: {finding.message}\n"
            f"Original code:\n{finding.snippet}\n\n"
            f"Fix suggestion: {finding.suggestion}\n\n"
            f"Output the corrected replacement code only."
        )

        try:
            fixed_code = await llm_call(
                "coder",
                _SYSTEM_PROMPT,
                user_prompt,
                temperature=0.1,
                max_tokens=512,
            )
        except Exception as e:
            console.print(f"[yellow][autofix] LLM fix failed for {finding.file}: {e}[/yellow]")
            continue

        fixed_code = fixed_code.strip()
        if not fixed_code:
            continue

        # Apply fix via str.replace
        new_source = original_source.replace(finding.snippet, fixed_code, 1)
        if new_source == original_source:
            console.print(f"[yellow][autofix] Replace had no effect for {finding.file}[/yellow]")
            continue

        file_path.write_text(new_source, encoding="utf-8")

        # Commit the single fix
        try:
            sha = git_tools.commit_changes(
                repo_path,
                files=[finding.file],
                message=f"fix({finding.category}): auto-fix {finding.file}:{finding.line}\n\n{finding.message[:200]}",
            )
            commit_hashes.append(sha[:8])
        except Exception as e:
            console.print(f"[yellow][autofix] Commit failed: {e}[/yellow]")
            continue

        fixes_applied += 1
        pr_rows.append(
            f"| `{finding.file}:{finding.line}` "
            f"| {finding.category} "
            f"| {finding.severity.value} "
            f"| {finding.message[:80]}... |"
        )
        console.print(f"[green][autofix] Fixed {finding.category} in {finding.file}:{finding.line}[/green]")

    # Push branch
    if fixes_applied > 0:
        git_tools.push_branch(repo_path, branch_name)

    # Build PR body
    pr_body = _build_pr_body(fixable[:fixes_applied], pr_rows, branch_name)

    # Open PR
    github_token = os.getenv("GITHUB_TOKEN", "")
    pr_url = ""
    if repo_url and fixes_applied > 0:
        pr_url = git_tools.open_pull_request(
            repo_url=repo_url,
            branch=branch_name,
            base_branch="main",
            title=f"[AutoPilot CI] Auto-fix {fixes_applied} issue(s)",
            body=pr_body,
            github_token=github_token,
        )

    return AutoFixResult(
        status=AgentStatus.DONE,
        fixes_applied=fixes_applied,
        branch_name=branch_name,
        commit_hashes=commit_hashes,
        pr_url=pr_url,
        pr_body=pr_body,
        duration_seconds=round(time.monotonic() - t0, 2),
    )


def _build_pr_body(
    findings: list,
    pr_rows: list[str],
    branch_name: str,
) -> str:
    """Build a markdown PR body describing all applied fixes.

    Args:
        findings: List of auto-fixed Finding objects.
        pr_rows: Pre-built markdown table rows.
        branch_name: Name of the fix branch.

    Returns:
        Markdown PR body string.
    """
    header = (
        "## 🤖 AutoPilot CI — Automated Fix Report\n\n"
        f"This PR was automatically generated by AutoPilot CI on branch `{branch_name}`.\n\n"
    )
    if not pr_rows:
        return header + "_No fixes were applied._"

    table = (
        "| Location | Category | Severity | Description |\n"
        "|----------|----------|----------|-------------|\n"
        + "\n".join(pr_rows)
    )

    footer = (
        "\n\n---\n"
        "_Generated by [AutoPilot CI](https://github.com/autopilot-ci) "
        "running on AMD MI300X with vLLM._"
    )

    return header + table + footer
