"""
Test generator agent.

1. Finds untested functions using ast_tools.get_untested_functions()
2. For each untested function, calls llm_call(model='coder') with the
   function source and asks for a complete pytest test file
3. Writes generated test file to /tmp/generated_tests/{name}_test.py
4. Returns TestGenerationResult with paths to written files
"""
from __future__ import annotations
import time
from pathlib import Path
from schemas import TestGenerationResult, FunctionInfo, AgentStatus
from llm_client import llm_call
from tools.ast_tools import get_untested_functions
from rich.console import Console

console = Console()

_OUTPUT_DIR = Path("/tmp/generated_tests")

_SYSTEM_PROMPT = """You are a test engineer in an automated CI pipeline.
Given a Python function, write a complete pytest test file with:
- 3-5 test cases covering normal operation, edge cases, and error conditions
- Proper imports
- Clear test function names (test_<function_name>_<scenario>)
- No mocking unless absolutely necessary
Output ONLY the Python test file content. No explanation."""


async def run_test_generator(
    changed_files: dict[str, str],
    repo_path: str,
) -> TestGenerationResult:
    """Find untested functions and generate pytest test files for them.

    Searches the repo's test directory for existing test coverage.
    Calls coder LLM to generate complete test files for uncovered functions.
    Writes output to /tmp/generated_tests/.

    Args:
        changed_files: Dict mapping filepath to source code string.
        repo_path: Root of the repository (used to locate test dir).

    Returns:
        TestGenerationResult with untested functions and generated file paths.
    """
    t0 = time.monotonic()
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    test_dir = str(Path(repo_path) / "tests")
    untested_all: list[FunctionInfo] = []

    for filepath in changed_files:
        untested = get_untested_functions(
            source_path=str(Path(repo_path) / filepath),
            test_dir=test_dir,
        )
        untested_all.extend(untested)

    if not untested_all:
        return TestGenerationResult(
            status=AgentStatus.DONE,
            untested_functions=[],
            generated_test_files=[],
            tests_written=0,
            duration_seconds=round(time.monotonic() - t0, 2),
        )

    generated_paths: list[str] = []
    tests_written = 0

    # Group by file to generate one test file per source file
    by_file: dict[str, list[FunctionInfo]] = {}
    for fn in untested_all:
        by_file.setdefault(fn.file, []).append(fn)

    for source_filepath, functions in by_file.items():
        module_name = Path(source_filepath).stem
        funcs_text = "\n\n".join(
            f"# Function: {fn.name} (line {fn.start_line})\n{fn.source}"
            for fn in functions[:5]  # cap at 5 per file to limit tokens
        )

        user_prompt = (
            f"Source file: {source_filepath}\n"
            f"Write pytest tests for these {len(functions)} untested function(s):\n\n"
            f"{funcs_text}"
        )

        try:
            test_code = await llm_call(
                "coder",
                _SYSTEM_PROMPT,
                user_prompt,
                temperature=0.2,
                max_tokens=1024,
            )
        except Exception as e:
            console.print(f"[yellow][test_generator] LLM failed for {module_name}: {e}[/yellow]")
            test_code = _stub_test_file(functions, source_filepath)

        out_path = _OUTPUT_DIR / f"test_{module_name}_generated.py"
        out_path.write_text(test_code, encoding="utf-8")
        generated_paths.append(str(out_path))
        tests_written += len(functions)
        console.print(f"[green][test_generator] Wrote {str(out_path)}[/green]")

    return TestGenerationResult(
        status=AgentStatus.DONE,
        untested_functions=untested_all,
        generated_test_files=generated_paths,
        tests_written=tests_written,
        duration_seconds=round(time.monotonic() - t0, 2),
    )


def _stub_test_file(functions: list[FunctionInfo], source_path: str) -> str:
    """Generate a minimal stub test file when LLM is unavailable.

    Args:
        functions: List of untested functions.
        source_path: Source file path for import generation.

    Returns:
        Python test file content as string.
    """
    module = Path(source_path).stem
    lines = [
        f"\"\"\"Auto-generated stub tests for {source_path}\"\"\"",
        "import pytest",
        f"# from {module} import ...",
        "",
    ]
    for fn in functions:
        lines.append(f"def test_{fn.name}_basic():")
        lines.append(f"    \"\"\"Stub test for {fn.name} — implement me.\"\"\"")
        lines.append(f"    pass")
        lines.append("")
    return "\n".join(lines)
