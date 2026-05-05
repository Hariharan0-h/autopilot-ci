"""
tools/dotnet_tools.py — .NET / C# static analysis via dotnet build.

Parses compiler output for errors and warnings and returns them as Findings.
Works without LLM — purely compiler-driven.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from schemas import Finding, Severity

# MSBuild diagnostic line pattern:
# path/to/File.cs(42,13): error CS1002: ; expected [project.csproj]
_DIAG_RE = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),\d+\):\s+"
    r"(?P<level>error|warning|info)\s+"
    r"(?P<code>CS\d+|[A-Z]+\d*):\s+"
    r"(?P<msg>.+?)(?:\s+\[.+\])?$",
    re.IGNORECASE,
)

_LEVEL_SEVERITY = {
    "error":   Severity.HIGH,
    "warning": Severity.MEDIUM,
    "info":    Severity.INFO,
}


def run_dotnet_build(repo_path: str) -> list[Finding]:
    """Run `dotnet build` on the repo and return compiler diagnostics as Findings.

    Finds the first *.sln or *.csproj in repo_path and builds it.
    Falls back gracefully if dotnet CLI is not installed.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        List of Finding objects for every error/warning the compiler emits.
    """
    root = Path(repo_path)

    # Prefer solution file; fall back to first csproj
    targets = list(root.rglob("*.sln")) or list(root.rglob("*.csproj"))
    if not targets:
        return []

    target = str(targets[0])

    try:
        result = subprocess.run(
            ["dotnet", "build", target, "--no-restore", "-v", "quiet",
             "/clp:NoSummary"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        # dotnet CLI not installed — skip silently
        return []
    except subprocess.TimeoutExpired:
        return [Finding(
            file="dotnet_build",
            line=0,
            severity=Severity.INFO,
            category="build_timeout",
            message="dotnet build timed out after 120 s — partial results only.",
        )]
    except Exception as e:
        return []

    findings: list[Finding] = []
    output = result.stdout + result.stderr

    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _DIAG_RE.match(line)
        if not m:
            continue

        file_path = m.group("file").strip()
        # Make path relative to repo root if possible
        try:
            file_path = str(Path(file_path).relative_to(root))
        except ValueError:
            pass

        severity = _LEVEL_SEVERITY.get(m.group("level").lower(), Severity.INFO)
        findings.append(Finding(
            file=file_path,
            line=int(m.group("line")),
            severity=severity,
            category=f"dotnet_{m.group('level').lower()}",
            message=f"{m.group('code')}: {m.group('msg').strip()}",
            auto_fixable=False,
        ))

    return findings
