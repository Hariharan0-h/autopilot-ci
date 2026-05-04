"""shell_tools.py — Safe subprocess executor with timeout and output capture."""

import asyncio
import subprocess
from dataclasses import dataclass


@dataclass
class ShellResult:
    """Result of a shell command execution."""
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def run_command(
    cmd: list[str],
    cwd: str = ".",
    timeout_seconds: int = 60,
    env: dict | None = None,
) -> ShellResult:
    """Run a command asynchronously with timeout. Never raises on non-zero exit.

    Captures stdout and stderr as strings. Sets timed_out=True if the process
    exceeds timeout_seconds.

    Args:
        cmd: Command and arguments as a list (e.g. ['bandit', '-f', 'json', 'file.py']).
        cwd: Working directory for the command.
        timeout_seconds: Kill process after this many seconds.
        env: Optional environment dict to pass to the process.

    Returns:
        ShellResult with returncode, stdout, stderr, timed_out.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
            return ShellResult(
                returncode=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ShellResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
                timed_out=True,
            )
    except FileNotFoundError:
        return ShellResult(
            returncode=127,
            stdout="",
            stderr=f"Command not found: {cmd[0]}",
        )
    except Exception as e:
        return ShellResult(
            returncode=1,
            stdout="",
            stderr=str(e),
        )


def run_command_sync(
    cmd: list[str],
    cwd: str = ".",
    timeout_seconds: int = 60,
) -> ShellResult:
    """Synchronous version of run_command for use in non-async contexts.

    Args:
        cmd: Command and arguments as a list.
        cwd: Working directory for the command.
        timeout_seconds: Kill process after this many seconds.

    Returns:
        ShellResult with returncode, stdout, stderr, timed_out.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return ShellResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_seconds}s",
            timed_out=True,
        )
    except FileNotFoundError:
        return ShellResult(
            returncode=127,
            stdout="",
            stderr=f"Command not found: {cmd[0]}",
        )
    except Exception as e:
        return ShellResult(
            returncode=1,
            stdout="",
            stderr=str(e),
        )
