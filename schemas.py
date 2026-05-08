"""
schemas.py — All Pydantic v2 data models for AutoPilot CI.
Every inter-agent data transfer uses these models. No raw dicts anywhere.
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import uuid
from datetime import datetime


# ─── Enums ────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class PipelineAction(str, Enum):
    AUTO_FIX = "AUTO_FIX"
    ESCALATE = "ESCALATE"
    DEPLOY = "DEPLOY"
    SKIP = "SKIP"

class AgentStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

class DeployStrategy(str, Enum):
    SKIP = "skip"
    ROLLING = "rolling"
    BLUE_GREEN = "blue_green"


# ─── Core finding models ───────────────────────────────────────────────────────

class Finding(BaseModel):
    """A single issue found by any analysis agent."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    file: str
    line: int
    severity: Severity
    category: str          # e.g. "sql_injection", "missing_test", "n_plus_1"
    message: str           # Human-readable description
    snippet: str = ""      # The offending code snippet
    suggestion: str = ""   # Concrete fix suggestion
    auto_fixable: bool = False


class FunctionInfo(BaseModel):
    """Metadata about a Python function extracted by ast_tools."""
    name: str
    file: str
    start_line: int
    end_line: int
    args: list[str] = []
    has_docstring: bool = False
    cyclomatic_complexity: int = 1
    source: str = ""       # Raw source of the function


# ─── Agent result models ───────────────────────────────────────────────────────

class CodeAnalysisResult(BaseModel):
    agent: str = "code_analyzer"
    status: AgentStatus = AgentStatus.DONE
    findings: list[Finding] = []
    files_analyzed: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


class TestGenerationResult(BaseModel):
    agent: str = "test_generator"
    status: AgentStatus = AgentStatus.DONE
    untested_functions: list[FunctionInfo] = []
    generated_test_files: list[str] = []  # Paths to written test files
    tests_written: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


class SecurityScanResult(BaseModel):
    agent: str = "security_scanner"
    status: AgentStatus = AgentStatus.DONE
    findings: list[Finding] = []
    bandit_score: float = 0.0   # 0.0 = clean, 10.0 = critical
    cve_hits: list[str] = []    # e.g. ["requests==2.18.0 (CVE-2023-32681)"]
    duration_seconds: float = 0.0
    error: Optional[str] = None


class PerfAnalysisResult(BaseModel):
    agent: str = "perf_analyzer"
    status: AgentStatus = AgentStatus.DONE
    findings: list[Finding] = []
    duration_seconds: float = 0.0
    error: Optional[str] = None


class AutoFixResult(BaseModel):
    agent: str = "autofix"
    status: AgentStatus = AgentStatus.DONE
    fixes_applied: int = 0
    branch_name: str = ""
    commit_hashes: list[str] = []
    pr_url: str = ""           # GitHub PR URL or empty string
    pr_body: str = ""          # Full PR markdown body
    duration_seconds: float = 0.0
    error: Optional[str] = None


class DeploymentResult(BaseModel):
    agent: str = "deployment"
    status: AgentStatus = AgentStatus.DONE
    strategy: DeployStrategy = DeployStrategy.SKIP
    log_lines: list[str] = []
    duration_seconds: float = 0.0
    error: Optional[str] = None


class DockerBuildResult(BaseModel):
    agent: str = "docker_scanner"
    status: AgentStatus = AgentStatus.DONE
    dockerfiles_found: list[str] = []
    compose_files_found: list[str] = []
    build_success: bool = False
    build_output: str = ""
    build_errors: str = ""
    image_tag: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None


# ─── Supervisor models ─────────────────────────────────────────────────────────

class SupervisorDecision(BaseModel):
    """Output of the supervisor agent after aggregating all analysis results."""
    action: PipelineAction
    summary: str                        # 2-3 sentence plain-English summary
    total_findings: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    auto_fixable_findings: list[Finding] = []
    escalation_findings: list[Finding] = []   # Needs human review
    confidence: float = 0.9             # 0.0–1.0


# ─── Pipeline event (streamed to dashboard) ────────────────────────────────────

class PipelineEvent(BaseModel):
    """Emitted to the asyncio.Queue at every meaningful pipeline step."""
    run_id: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    agent: str          # "supervisor" | "code_analyzer" | "pipeline" | etc.
    status: AgentStatus
    message: str
    payload: Optional[dict] = None   # Optional structured data


# ─── Pipeline run ──────────────────────────────────────────────────────────────

class PipelineRun(BaseModel):
    """Tracks the full state of one pipeline execution."""
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    repo_path: str
    base_commit: str
    head_commit: str
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: Optional[str] = None
    status: AgentStatus = AgentStatus.WAITING
    code_result: Optional[CodeAnalysisResult] = None
    test_result: Optional[TestGenerationResult] = None
    security_result: Optional[SecurityScanResult] = None
    perf_result: Optional[PerfAnalysisResult] = None
    supervisor_decision: Optional[SupervisorDecision] = None
    autofix_result: Optional[AutoFixResult] = None
    deployment_result: Optional[DeploymentResult] = None
    docker_result: Optional[DockerBuildResult] = None


# --- Payload models ----------------------------------------------------------

class SubmitPayload(BaseModel):
    """Accepted by POST /submit from the dashboard UI."""
    path: str = Field(..., description="GitHub URL (https://github.com/owner/repo) or local filesystem path")
    base: str = Field("HEAD~1", description="Base branch or commit SHA")
    head: str = Field("HEAD", description="Head branch or commit SHA")
    mock: bool = Field(False, description="If true, runs demo simulation instead of real pipeline")
    full_scan: bool = Field(False, description="If true, scan ALL source files not just the diff")


class WebhookPayload(BaseModel):
    """Accepted by POST /webhook. Supports both GitHub-style and simplified."""
    repo_path: str = Field(..., description="Local path or clone URL of the repo")
    base: str = Field(..., description="Base commit SHA (before push)")
    head: str = Field(..., description="Head commit SHA (after push)")
    branch: str = "main"
    repo_url: str = ""   # For PR creation via GitHub API
    full_scan: bool = Field(False, description="If true, scan ALL source files not just the diff")
