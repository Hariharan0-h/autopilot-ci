"""
Performance analyzer agent.

1. Uses ast_tools.find_nested_loops() and find_db_calls_in_loops()
2. Flags functions > 50 lines
3. Calls llm_call(model='perf') to suggest a concrete optimized version
   for each finding
4. Returns PerfAnalysisResult
"""
from __future__ import annotations
import time
from schemas import PerfAnalysisResult, Finding, Severity, AgentStatus
from llm_client import llm_call
from tools.ast_tools import (
    find_nested_loops, find_db_calls_in_loops, get_functions,
)
from rich.console import Console

console = Console()

_SYSTEM_PROMPT = """You are a performance engineer in an automated CI pipeline.
Given a Python performance issue, write a 1-2 sentence explanation and a concrete
suggestion for how to optimize the code. Be specific — mention the data structure
or algorithm change needed. Output only the suggestion, no headers."""


async def run_perf_analyzer(
    changed_files: dict[str, str],
) -> PerfAnalysisResult:
    """Analyze changed files for performance issues.

    Detects nested loops (O(n²)), DB calls inside loops (N+1 queries),
    and overly long functions. Calls perf LLM for concrete optimization suggestions.

    Args:
        changed_files: Dict mapping filepath to source code string.

    Returns:
        PerfAnalysisResult with findings list.
    """
    t0 = time.monotonic()
    findings: list[Finding] = []

    for filepath, source in changed_files.items():
        # Detect nested loops
        nested = find_nested_loops(source)
        for hit in nested:
            try:
                suggestion = await llm_call(
                    "perf",
                    _SYSTEM_PROMPT,
                    f"File: {filepath}, line {hit['line']}\n"
                    f"Issue: Nested loop (depth {hit['depth']}) — O(n²) complexity.\n"
                    f"Code: {hit['snippet']}",
                    temperature=0.2,
                    max_tokens=256,
                )
            except Exception:
                suggestion = "Replace inner loop with a set or dict for O(1) lookups."

            findings.append(Finding(
                file=filepath,
                line=hit["line"],
                severity=Severity.MEDIUM,
                category="nested_loop",
                message=(
                    f"Nested loop at line {hit['line']} has O(n²) complexity "
                    f"(depth {hit['depth']}). {suggestion}"
                ),
                snippet=hit["snippet"],
                suggestion=suggestion,
                auto_fixable=False,
            ))

        # Detect N+1 DB calls in loops
        db_hits = find_db_calls_in_loops(source)
        for hit in db_hits:
            try:
                suggestion = await llm_call(
                    "perf",
                    _SYSTEM_PROMPT,
                    f"File: {filepath}, line {hit['line']}\n"
                    f"Issue: Database call inside a loop (N+1 query pattern).\n"
                    f"Code: {hit['snippet']}",
                    temperature=0.2,
                    max_tokens=256,
                )
            except Exception:
                suggestion = "Batch the query outside the loop using IN clauses or eager loading."

            findings.append(Finding(
                file=filepath,
                line=hit["line"],
                severity=Severity.MEDIUM,
                category="n_plus_1",
                message=(
                    f"Database call inside loop at line {hit['line']} "
                    f"causes N+1 queries. {suggestion}"
                ),
                snippet=hit["snippet"],
                suggestion=suggestion,
                auto_fixable=False,
            ))

        # Flag long functions
        functions = get_functions(source, filepath=filepath)
        for fn in functions:
            fn_length = fn.end_line - fn.start_line + 1
            if fn_length > 50:
                findings.append(Finding(
                    file=filepath,
                    line=fn.start_line,
                    severity=Severity.LOW,
                    category="long_function",
                    message=(
                        f"Function `{fn.name}` is {fn_length} lines long (threshold: 50). "
                        "Consider splitting into smaller helper functions."
                    ),
                    snippet=fn.source[:200],
                    suggestion="Extract logical sub-operations into named helper functions.",
                    auto_fixable=False,
                ))

    return PerfAnalysisResult(
        status=AgentStatus.DONE,
        findings=findings,
        duration_seconds=round(time.monotonic() - t0, 2),
    )
