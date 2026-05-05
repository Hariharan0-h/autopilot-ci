"""
docker_scanner.py — Detects Docker artefacts and runs docker build on the codebase.

1. Recursively finds all Dockerfile* and docker-compose*.yml in the repo
2. Runs `docker build` against the first Dockerfile found (or `docker compose build`)
3. Returns DockerBuildResult with build success, output, and image tag
"""
from __future__ import annotations
import asyncio
import time
from pathlib import Path

from schemas import DockerBuildResult, AgentStatus
from rich.console import Console

console = Console()

_BUILD_TIMEOUT = 300  # seconds — docker builds can be slow


async def run_docker_scanner(repo_path: str) -> DockerBuildResult:
    """Scan repo for Docker files and attempt a build.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        DockerBuildResult with found files, build success, and output.
    """
    t0 = time.monotonic()
    root = Path(repo_path)

    # ── Discover Docker artefacts ──────────────────────────────────────────────
    _skip = {".git", "node_modules", "__pycache__", "bin", "obj"}

    dockerfiles = [
        str(f.relative_to(root))
        for f in root.rglob("Dockerfile*")
        if f.is_file() and not any(p in _skip for p in f.parts)
    ]
    compose_files = [
        str(f.relative_to(root))
        for f in root.rglob("docker-compose*.yml")
        if f.is_file() and not any(p in _skip for p in f.parts)
    ]

    if not dockerfiles and not compose_files:
        return DockerBuildResult(
            status=AgentStatus.DONE,
            dockerfiles_found=[],
            compose_files_found=[],
            build_success=False,
            build_output="No Dockerfile or docker-compose*.yml found in repository.",
            duration_seconds=round(time.monotonic() - t0, 2),
        )

    # ── Run docker build ───────────────────────────────────────────────────────
    build_success = False
    build_output = ""
    build_errors = ""
    image_tag = ""

    try:
        if dockerfiles:
            # Build with the first (root-level preferred) Dockerfile
            dockerfiles_sorted = sorted(dockerfiles, key=lambda p: p.count("/"))
            dockerfile = dockerfiles_sorted[0]
            context_dir = str(root / Path(dockerfile).parent)
            image_tag = "autopilot-ci-build:latest"

            proc = await asyncio.create_subprocess_exec(
                "docker", "build",
                "-f", str(root / dockerfile),
                "-t", image_tag,
                context_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_BUILD_TIMEOUT
            )
            build_output = stdout.decode("utf-8", errors="replace")
            build_errors = stderr.decode("utf-8", errors="replace")
            build_success = proc.returncode == 0
            if not build_success:
                image_tag = ""

        elif compose_files:
            compose_file = sorted(compose_files, key=lambda p: p.count("/"))[0]
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose",
                "-f", str(root / compose_file),
                "build",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_BUILD_TIMEOUT
            )
            build_output = stdout.decode("utf-8", errors="replace")
            build_errors = stderr.decode("utf-8", errors="replace")
            build_success = proc.returncode == 0

    except asyncio.TimeoutError:
        build_errors = f"Docker build timed out after {_BUILD_TIMEOUT}s."
    except FileNotFoundError:
        build_errors = "docker CLI not found — is Docker installed and on PATH?"
    except Exception as e:
        build_errors = str(e)

    return DockerBuildResult(
        status=AgentStatus.DONE if build_success else AgentStatus.FAILED,
        dockerfiles_found=dockerfiles,
        compose_files_found=compose_files,
        build_success=build_success,
        build_output=build_output[-3000:],   # cap output size
        build_errors=build_errors[-2000:],
        image_tag=image_tag,
        duration_seconds=round(time.monotonic() - t0, 2),
    )
