"""
Security scanner agent.

1. Runs sast_tools.run_bandit() on each changed file
2. Runs sast_tools.check_requirements_cve() if requirements.txt changed
3. Calls llm_call(model='security') to write a plain-English explanation
   of each finding suitable for a PR comment
4. Returns SecurityScanResult
"""
from __future__ import annotations
import time
from pathlib import Path
from schemas import SecurityScanResult, Finding, Severity, AgentStatus
from llm_client import llm_call
from tools.sast_tools import run_bandit, check_requirements_cve
from rich.console import Console

console = Console()

_SYSTEM_PROMPT = """You are a security engineer reviewing code in an automated CI pipeline.
Given a security finding from static analysis, write a 1-2 sentence plain-English explanation
that a developer can understand. Describe the risk and the recommended fix concisely.
Do not use jargon. Output only the explanation, no headers or bullet points."""


def _compute_bandit_score(findings: list[Finding]) -> float:
    """Compute a 0.0–10.0 bandit risk score from findings.

    Args:
        findings: List of Finding objects from bandit.

    Returns:
        Float score where 10.0 = maximum risk.
    """
    if not findings:
        return 0.0
    weights = {
        Severity.CRITICAL: 10.0,
        Severity.HIGH: 7.0,
        Severity.MEDIUM: 4.0,
        Severity.LOW: 1.0,
        Severity.INFO: 0.5,
    }
    total = sum(weights.get(f.severity, 0) for f in findings)
    return min(round(total / max(len(findings), 1), 1), 10.0)


async def run_security_scanner(
    changed_files: dict[str, str],
    repo_path: str,
) -> SecurityScanResult:
    """Run static security analysis on changed files.

    Runs bandit on each .py file and checks requirements.txt for CVEs.
    Calls security LLM to produce human-readable PR comment text per finding.

    Args:
        changed_files: Dict mapping filepath to source code string.
        repo_path: Root of the repository (used to locate requirements.txt).

    Returns:
        SecurityScanResult with findings, bandit_score, and cve_hits.
    """
    t0 = time.monotonic()
    all_findings: list[Finding] = []
    cve_hits: list[str] = []

    # Run bandit on each changed Python file
    for filepath in changed_files:
        abs_path = str(Path(repo_path) / filepath)
        bandit_findings = run_bandit(abs_path)
        for f in bandit_findings:
            # Mark SQL injection findings as auto-fixable if they have a suggestion
            if "sql" in f.category.lower() and f.suggestion:
                f.auto_fixable = True
            all_findings.append(f)

    # Check requirements.txt CVEs
    req_path = Path(repo_path) / "requirements.txt"
    if req_path.exists():
        cve_hits = check_requirements_cve(str(req_path))
        for cve in cve_hits:
            all_findings.append(Finding(
                file="requirements.txt",
                line=0,
                severity=Severity.HIGH,
                category="vulnerable_dependency",
                message=f"Vulnerable dependency detected: {cve}",
                snippet=cve.split(" ")[0],
                suggestion="Upgrade to a patched version of this package.",
                auto_fixable=False,
            ))

    # Enrich each finding with LLM explanation
    enriched: list[Finding] = []
    for finding in all_findings:
        try:
            explanation = await llm_call(
                "security",
                _SYSTEM_PROMPT,
                f"Finding: {finding.message}\nCode: {finding.snippet}\n"
                f"Category: {finding.category}\nSeverity: {finding.severity.value}",
                temperature=0.1,
                max_tokens=256,
            )
            finding.message = explanation or finding.message
        except Exception:
            pass  # Keep original message
        enriched.append(finding)

    bandit_score = _compute_bandit_score(enriched)

    return SecurityScanResult(
        status=AgentStatus.DONE,
        findings=enriched,
        bandit_score=bandit_score,
        cve_hits=cve_hits,
        duration_seconds=round(time.monotonic() - t0, 2),
    )
