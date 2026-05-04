"""
llm_client.py — Single wrapper for all LLM calls in AutoPilot CI.

All agents call llm_call() or llm_call_json(). This module handles:
  - Reading model config from models.yaml
  - Mock mode (returns hardcoded realistic responses without GPU)
  - Retry with exponential backoff (3 attempts)
  - Token count logging via rich
  - Structured JSON output via llm_call_json()
"""

from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI, APIError
from rich.console import Console

load_dotenv()
console = Console()

# ─── Config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load and return models.yaml, expanding env vars in endpoint values."""
    config_path = Path(__file__).parent / "config" / "models.yaml"
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    for name, cfg in raw.get("models", {}).items():
        if "endpoint" in cfg:
            cfg["endpoint"] = os.path.expandvars(cfg["endpoint"])
    return raw

_CONFIG: dict = _load_config()


def get_model_config(model_name: str) -> dict:
    """Return config dict for a named model (e.g. 'coder', 'supervisor').

    Args:
        model_name: Key from models.yaml (supervisor, coder, security, perf, deploy).

    Raises:
        ValueError: If model_name is not found in config.
    """
    models = _CONFIG.get("models", {})
    if model_name not in models:
        raise ValueError(
            f"Unknown model name '{model_name}'. "
            f"Available: {list(models.keys())}"
        )
    return models[model_name]


def is_mock(model_name: str) -> bool:
    """Return True if this model is configured for mock mode."""
    return get_model_config(model_name).get("mock", False)


# ─── Mock responses ────────────────────────────────────────────────────────────

MOCK_RESPONSES: dict[str, str] = {
    "supervisor": json.dumps({
        "action": "AUTO_FIX",
        "summary": (
            "Found 3 issues across 2 files: one SQL injection (critical), "
            "one missing test (medium), and one O(n²) loop (medium). "
            "All issues are auto-fixable with high confidence."
        ),
        "total_findings": 3,
        "critical_count": 1,
        "high_count": 0,
        "medium_count": 2,
        "low_count": 0,
        "confidence": 0.92,
    }),
    "coder": (
        "Analysis complete. Found 2 issues:\n"
        "1. Function `get_user` at line 12 has cyclomatic complexity of 14 "
        "(threshold: 10). Consider splitting into smaller functions.\n"
        "2. Public function `process_data` at line 45 is missing a docstring."
    ),
    "security": json.dumps({
        "bandit_findings": [
            {
                "file": "app.py",
                "line": 8,
                "severity": "critical",
                "category": "sql_injection",
                "message": "Possible SQL injection via string formatting",
                "snippet": 'query = f"SELECT * FROM users WHERE id = {user_id}"',
                "suggestion": (
                    "Use parameterized queries: "
                    "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
                ),
                "auto_fixable": True,
            }
        ],
        "cve_hits": ["requests==2.18.0 (CVE-2023-32681: SSRF via crafted URL)"],
        "bandit_score": 8.5,
    }),
    "perf": (
        "Performance issues found:\n"
        "1. Nested loop at lines 23-27 in app.py has O(n²) complexity. "
        "For n=1000, this executes 1,000,000 iterations.\n"
        "Suggestion: Use a set or dict for O(1) lookups in the inner loop."
    ),
    "test_generator": (
        "Generated tests for 2 untested functions:\n"
        "- `calculate_discount` (utils.py:15): 3 test cases written\n"
        "- `validate_email` (utils.py:34): 4 test cases written\n"
        "Output: /tmp/generated_tests/test_utils_generated.py"
    ),
    "autofix": (
        "Applied 2 fixes:\n"
        "1. SQL injection → parameterized query (app.py:8)\n"
        "2. O(n²) loop → dict-based O(n) lookup (app.py:23-27)\n"
        "Branch: autopilot/fix-1234567\n"
        "PR: https://github.com/example/repo/pull/42"
    ),
    "deploy": (
        "Strategy selected: blue_green\n"
        "Deploy log:\n"
        "  [1/4] Building new image...\n"
        "  [2/4] Running smoke tests...\n"
        "  [3/4] Switching traffic to green environment...\n"
        "  [4/4] Deploy complete. Old environment standing by."
    ),
}


# ─── OpenAI client factory ─────────────────────────────────────────────────────

def _make_client(endpoint: str) -> AsyncOpenAI:
    """Create an AsyncOpenAI client pointed at the given vLLM endpoint."""
    return AsyncOpenAI(
        base_url=endpoint,
        api_key=os.getenv("VLLM_API_KEY", "not-required"),
        timeout=120.0,
    )


# ─── Core call functions ───────────────────────────────────────────────────────

async def llm_call(
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """Make a single LLM call, returning the assistant's response as a string.

    Uses mock response if mock: true in models.yaml.
    Retries up to 3 times with exponential backoff on API errors.

    Args:
        model_name: Key from models.yaml (supervisor, coder, security, perf, deploy).
        system_prompt: System message content.
        user_prompt: User message content.
        temperature: Sampling temperature (default 0.2 for deterministic output).
        max_tokens: Maximum tokens in response.

    Returns:
        Assistant response string.
    """
    if is_mock(model_name):
        mock_key = model_name if model_name in MOCK_RESPONSES else "coder"
        mock_text = MOCK_RESPONSES[mock_key]
        console.print(f"[dim][MOCK] {model_name} → {len(mock_text)} chars[/dim]")
        await asyncio.sleep(0.1)
        return mock_text

    cfg = get_model_config(model_name)
    client = _make_client(cfg["endpoint"])
    model_id = cfg["model"]

    for attempt in range(1, 4):
        try:
            t0 = time.monotonic()
            response = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            elapsed = time.monotonic() - t0
            usage = response.usage
            console.print(
                f"[dim][LLM] {model_name} | "
                f"in={usage.prompt_tokens} out={usage.completion_tokens} "
                f"tok | {elapsed:.1f}s[/dim]"
            )
            return response.choices[0].message.content or ""

        except APIError as e:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            console.print(
                f"[yellow][LLM] {model_name} attempt {attempt} failed "
                f"({e}), retrying in {wait}s...[/yellow]"
            )
            await asyncio.sleep(wait)

    return ""  # unreachable


async def llm_call_json(
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Like llm_call but expects and parses a JSON response.

    Appends JSON-only instruction to system prompt automatically.
    Strips markdown code fences before parsing.
    Retries up to 3 times on JSON parse failure.

    Args:
        model_name: Key from models.yaml.
        system_prompt: System message content.
        user_prompt: User message content.
        temperature: Sampling temperature (default 0.1 for structured output).
        max_tokens: Maximum tokens in response.

    Returns:
        Parsed dict from JSON response.

    Raises:
        ValueError: If response is not valid JSON after 3 attempts.
    """
    json_system = (
        system_prompt.rstrip() + "\n\n"
        "IMPORTANT: Respond with valid JSON only. "
        "Do not include any text, explanation, or markdown code fences "
        "before or after the JSON object."
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        raw = await llm_call(
            model_name, json_system, user_prompt, temperature, max_tokens
        )
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            last_error = e
            console.print(
                f"[yellow][LLM] JSON parse failed attempt {attempt}: {e}[/yellow]"
            )
            if not is_mock(model_name):
                await asyncio.sleep(1)

    raise ValueError(
        f"llm_call_json: failed to get valid JSON from {model_name} "
        f"after 3 attempts. Last error: {last_error}"
    )
