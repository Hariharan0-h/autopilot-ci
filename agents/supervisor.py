"""
Supervisor agent — aggregates all 4 analysis results and decides action.

Uses llm_call_json with model='supervisor'. System prompt instructs it to
weigh severity counts and output a SupervisorDecision JSON matching the schema.
If any finding has severity=critical → action must be AUTO_FIX (not ESCALATE)
unless the fix is ambiguous (no suggestion provided).
Fallback: if LLM fails, derives decision deterministically from severity counts.
"""
from __future__ import annotations
import time
from schemas import (
    CodeAnalysisResult, TestGenerationResult, SecurityScanResult,
    PerfAnalysisResult, SupervisorDecision, PipelineAction,
    Severity, Finding, AgentStatus,
)
from llm_client import llm_call_json
from rich.console import Console

console = Console()

_SYSTEM_PROMPT = """You are the Supervisor agent in AutoPilot CI.
You receive structured analysis results from 4 parallel agents and decide what to do.

Your output MUST be a JSON object with exactly these keys:
{
  "action": one of "AUTO_FIX" | "ESCALATE" | "DEPLOY" | "SKIP",
  "summary": "2-3 sentence plain English summary",
  "total_findings": integer,
  "critical_count": integer,
  "high_count": integer,
  "medium_count": integer,
  "low_count": integer,
  "confidence": float between 0.0 and 1.0
}

Decision rules:
- CRITICAL severity with auto_fixable=true → AUTO_FIX
- CRITICAL severity with auto_fixable=false (no suggestion) → ESCALATE
- HIGH severity only, any auto_fixable=false → ESCALATE
- HIGH severity only, all auto_fixable=true → AUTO_FIX
- MEDIUM/LOW only → DEPLOY
- No findings → DEPLOY
"""


def _deterministic_decision(
    code_result: CodeAnalysisResult,
    test_result: TestGenerationResult,
    security_result: SecurityScanResult,
    perf_result: PerfAnalysisResult,
) -> SupervisorDecision:
    """Derive SupervisorDecision deterministically without LLM.

    Args:
        code_result: Code analysis agent result.
        test_result: Test generator agent result.
        security_result: Security scanner agent result.
        perf_result: Performance analyzer agent result.

    Returns:
        SupervisorDecision based on severity counts.
    """
    all_findings: list[Finding] = (
        code_result.findings
        + security_result.findings
        + perf_result.findings
    )

    critical = sum(1 for f in all_findings if f.severity == Severity.CRITICAL)
    high = sum(1 for f in all_findings if f.severity == Severity.HIGH)
    medium = sum(1 for f in all_findings if f.severity == Severity.MEDIUM)
    low = sum(1 for f in all_findings if f.severity == Severity.LOW)

    auto_fixable = [f for f in all_findings if f.auto_fixable]
    escalations = [f for f in all_findings if not f.auto_fixable]

    if critical > 0:
        has_ambiguous = any(
            f.severity == Severity.CRITICAL and not f.auto_fixable
            for f in all_findings
        )
        action = PipelineAction.ESCALATE if has_ambiguous else PipelineAction.AUTO_FIX
        summary = (
            f"Found {critical} critical issue(s). "
            + ("Manual review required — fix suggestion unavailable."
               if has_ambiguous
               else f"{len(auto_fixable)} auto-fixable finding(s) queued.")
        )
    elif high > 0:
        has_non_fixable = any(
            f.severity == Severity.HIGH and not f.auto_fixable
            for f in all_findings
        )
        action = PipelineAction.ESCALATE if has_non_fixable else PipelineAction.AUTO_FIX
        summary = (
            f"Found {high} high-severity issue(s). "
            + ("Escalating — some fixes require human review."
               if has_non_fixable
               else "All auto-fixable. Queuing auto-fix.")
        )
    elif medium > 0 or low > 0:
        action = PipelineAction.DEPLOY
        summary = (
            f"Found {medium} medium and {low} low-severity issue(s). "
            "Risk is acceptable — proceeding to deploy."
        )
    else:
        action = PipelineAction.DEPLOY
        summary = "No issues found across all analysis agents. Deploying."

    return SupervisorDecision(
        action=action,
        summary=summary,
        total_findings=len(all_findings),
        critical_count=critical,
        high_count=high,
        medium_count=medium,
        low_count=low,
        auto_fixable_findings=auto_fixable,
        escalation_findings=escalations,
        confidence=0.95,
    )


async def run_supervisor(
    code_result: CodeAnalysisResult,
    test_result: TestGenerationResult,
    security_result: SecurityScanResult,
    perf_result: PerfAnalysisResult,
) -> SupervisorDecision:
    """Aggregate all findings and return a SupervisorDecision.

    Calls LLM (supervisor model) with full context. Falls back to
    deterministic decision if LLM fails or returns invalid data.

    Args:
        code_result: Code analysis agent result.
        test_result: Test generator agent result.
        security_result: Security scanner agent result.
        perf_result: Performance analyzer agent result.

    Returns:
        SupervisorDecision with action, summary, and finding breakdowns.
    """
    t0 = time.monotonic()

    all_findings = (
        code_result.findings
        + security_result.findings
        + perf_result.findings
    )

    user_prompt = f"""
Code Analysis: {code_result.files_analyzed} files analyzed, {len(code_result.findings)} findings.
Findings: {[f.model_dump() for f in code_result.findings]}

Test Generation: {len(test_result.untested_functions)} untested functions found.

Security Scan: bandit_score={security_result.bandit_score}, cve_hits={security_result.cve_hits}
Security findings: {[f.model_dump() for f in security_result.findings]}

Performance Analysis: {len(perf_result.findings)} findings.
Perf findings: {[f.model_dump() for f in perf_result.findings]}

Total: {len(all_findings)} findings.
Auto-fixable: {sum(1 for f in all_findings if f.auto_fixable)}.

Decide the pipeline action.
"""

    try:
        data = await llm_call_json("supervisor", _SYSTEM_PROMPT, user_prompt)
        action_str = data.get("action", "DEPLOY")
        try:
            action = PipelineAction(action_str)
        except ValueError:
            action = PipelineAction.DEPLOY

        auto_fixable = [f for f in all_findings if f.auto_fixable]
        escalations = [f for f in all_findings if not f.auto_fixable]

        return SupervisorDecision(
            action=action,
            summary=data.get("summary", "LLM decision."),
            total_findings=data.get("total_findings", len(all_findings)),
            critical_count=data.get("critical_count", 0),
            high_count=data.get("high_count", 0),
            medium_count=data.get("medium_count", 0),
            low_count=data.get("low_count", 0),
            auto_fixable_findings=auto_fixable,
            escalation_findings=escalations,
            confidence=float(data.get("confidence", 0.9)),
        )
    except Exception as e:
        console.print(f"[yellow][supervisor] LLM failed ({e}), using deterministic fallback[/yellow]")
        decision = _deterministic_decision(code_result, test_result, security_result, perf_result)
        return decision
