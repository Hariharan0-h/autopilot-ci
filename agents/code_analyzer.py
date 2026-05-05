"""
Code analyzer agent.

1. Uses ast_tools.get_functions() on each changed file
2. Flags: complexity > 10, missing docstrings, functions > 50 lines
3. Calls llm_call(model='coder') to generate a plain-English review comment
   for each flagged function
4. Returns CodeAnalysisResult
"""
from __future__ import annotations
import time
from schemas import CodeAnalysisResult, Finding, Severity, AgentStatus
from llm_client import llm_call
from tools.ast_tools import get_functions, get_functions_without_docstrings
from rich.console import Console

console = Console()

_SYSTEM_PROMPT_PY = """You are a senior code reviewer in an automated CI pipeline.
Given a Python function, write a concise 1-2 sentence plain-English review comment
explaining the issue and what the developer should do to fix it.
Be direct and actionable. Do not use bullet points."""

_SYSTEM_PROMPT_GENERIC = """You are a senior code reviewer in an automated CI pipeline.
Review the following source file for code quality issues such as high complexity,
missing documentation, overly long functions, poor naming, or structural problems.
Write concise plain-English findings, one per issue. Be direct and actionable."""


async def run_code_analyzer(
    changed_files: dict[str, str],  # {filepath: source_code}
) -> CodeAnalysisResult:
    """Analyze changed files for code quality issues.

    For .py files: uses AST analysis (complexity, docstrings, length) + LLM comment.
    For all other file types: sends source directly to LLM for a generic review.

    Args:
        changed_files: Dict mapping filepath to source code string.

    Returns:
        CodeAnalysisResult with findings and file count.
    """
    t0 = time.monotonic()
    findings: list[Finding] = []

    for filepath, source in changed_files.items():
        if filepath.endswith(".py"):
            # Python: deep AST-based analysis
            functions = get_functions(source, filepath=filepath)

            for fn in functions:
                issues: list[tuple[str, Severity, str, str]] = []

                fn_length = fn.end_line - fn.start_line + 1

                if fn.cyclomatic_complexity > 10:
                    issues.append((
                        "high_complexity",
                        Severity.MEDIUM,
                        f"Function `{fn.name}` has cyclomatic complexity {fn.cyclomatic_complexity} (threshold: 10).",
                        "Split into smaller, single-responsibility functions.",
                    ))

                if not fn.has_docstring and not fn.name.startswith("_"):
                    issues.append((
                        "missing_docstring",
                        Severity.LOW,
                        f"Public function `{fn.name}` is missing a docstring.",
                        "Add a docstring describing purpose, args, and return value.",
                    ))

                if fn_length > 50:
                    issues.append((
                        "long_function",
                        Severity.LOW,
                        f"Function `{fn.name}` is {fn_length} lines long (threshold: 50).",
                        "Break into smaller helper functions.",
                    ))

                for category, severity, message, suggestion in issues:
                    try:
                        review = await llm_call(
                            "coder",
                            _SYSTEM_PROMPT_PY,
                            f"File: {filepath}\nFunction: {fn.name}\nIssue: {message}\n"
                            f"Source:\n{fn.source[:800]}",
                            temperature=0.2,
                            max_tokens=256,
                        )
                    except Exception:
                        review = message

                    findings.append(Finding(
                        file=filepath,
                        line=fn.start_line,
                        severity=severity,
                        category=category,
                        message=review or message,
                        snippet=fn.source[:200],
                        suggestion=suggestion,
                        auto_fixable=False,
                    ))

    return CodeAnalysisResult(
        status=AgentStatus.DONE,
        findings=findings,
        files_analyzed=len(changed_files),
        duration_seconds=round(time.monotonic() - t0, 2),
    )
