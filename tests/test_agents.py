"""
tests/test_agents.py — Integration tests for all agents in mock mode.
All tests run with mock: true (no GPU or vLLM required).
"""
import pytest
import asyncio
from schemas import (
    CodeAnalysisResult, TestGenerationResult, SecurityScanResult,
    PerfAnalysisResult, SupervisorDecision, AutoFixResult, DeploymentResult,
    AgentStatus, PipelineAction, Finding, Severity, SupervisorDecision,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_FILES = {
    "app.py": '''
import sqlite3

def get_user(user_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchone()

def find_duplicates(items):
    duplicates = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j and items[i] == items[j]:
                if items[i] not in duplicates:
                    duplicates.append(items[i])
    return duplicates
''',
}

MOCK_FINDING = Finding(
    file="app.py",
    line=8,
    severity=Severity.CRITICAL,
    category="sql_injection",
    message="SQL injection via f-string",
    snippet='query = f"SELECT * FROM users WHERE id = {user_id}"',
    suggestion="Use parameterized queries",
    auto_fixable=True,
)


@pytest.fixture
def mock_supervisor_decision():
    return SupervisorDecision(
        action=PipelineAction.AUTO_FIX,
        summary="Found 1 critical auto-fixable issue.",
        total_findings=1,
        critical_count=1,
        auto_fixable_findings=[MOCK_FINDING],
        escalation_findings=[],
        confidence=0.92,
    )


# ─── Code analyzer ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_code_analyzer_returns_correct_schema():
    from agents.code_analyzer import run_code_analyzer
    result = await run_code_analyzer(SAMPLE_FILES)
    assert isinstance(result, CodeAnalysisResult)
    assert result.status == AgentStatus.DONE
    assert result.files_analyzed == len(SAMPLE_FILES)
    assert isinstance(result.findings, list)
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_code_analyzer_empty_files():
    from agents.code_analyzer import run_code_analyzer
    result = await run_code_analyzer({})
    assert isinstance(result, CodeAnalysisResult)
    assert result.files_analyzed == 0
    assert result.findings == []


# ─── Test generator ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_test_generator_returns_correct_schema(tmp_path):
    # Create a minimal repo structure
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text(SAMPLE_FILES["app.py"])

    from agents.test_generator import run_test_generator
    result = await run_test_generator(SAMPLE_FILES, str(tmp_path))
    assert isinstance(result, TestGenerationResult)
    assert result.status == AgentStatus.DONE
    assert isinstance(result.generated_test_files, list)
    assert isinstance(result.untested_functions, list)


# ─── Security scanner ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_security_scanner_returns_correct_schema(tmp_path):
    (tmp_path / "app.py").write_text(SAMPLE_FILES["app.py"])
    (tmp_path / "requirements.txt").write_text("requests==2.18.0\n")

    from agents.security_scanner import run_security_scanner
    files = {"app.py": SAMPLE_FILES["app.py"]}
    result = await run_security_scanner(files, str(tmp_path))
    assert isinstance(result, SecurityScanResult)
    assert result.status == AgentStatus.DONE
    assert isinstance(result.findings, list)
    assert isinstance(result.cve_hits, list)
    assert result.bandit_score >= 0.0
    # CVE for requests==2.18.0 should be detected
    assert any("requests" in h for h in result.cve_hits)


# ─── Perf analyzer ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_perf_analyzer_returns_correct_schema():
    from agents.perf_analyzer import run_perf_analyzer
    result = await run_perf_analyzer(SAMPLE_FILES)
    assert isinstance(result, PerfAnalysisResult)
    assert result.status == AgentStatus.DONE
    assert isinstance(result.findings, list)
    # The nested loop in app.py should be detected
    nested_findings = [f for f in result.findings if f.category == "nested_loop"]
    assert len(nested_findings) >= 1


# ─── Supervisor ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supervisor_returns_correct_schema():
    from agents.supervisor import run_supervisor

    code = CodeAnalysisResult(findings=[])
    test = TestGenerationResult()
    security = SecurityScanResult(findings=[MOCK_FINDING], bandit_score=8.5)
    perf = PerfAnalysisResult(findings=[])

    result = await run_supervisor(code, test, security, perf)
    assert isinstance(result, SupervisorDecision)
    assert isinstance(result.action, PipelineAction)
    assert isinstance(result.summary, str)
    assert len(result.summary) > 0
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_supervisor_critical_finding_leads_to_auto_fix():
    from agents.supervisor import run_supervisor

    code = CodeAnalysisResult(findings=[])
    test = TestGenerationResult()
    security = SecurityScanResult(
        findings=[MOCK_FINDING],  # critical + auto_fixable
        bandit_score=8.5,
    )
    perf = PerfAnalysisResult(findings=[])

    result = await run_supervisor(code, test, security, perf)
    # Critical + auto_fixable should → AUTO_FIX
    assert result.action == PipelineAction.AUTO_FIX


# ─── Deployment ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deployment_returns_correct_schema(tmp_path, mock_supervisor_decision):
    from agents.deployment import run_deployment
    result = await run_deployment(
        changed_files=["app.py"],
        supervisor_decision=mock_supervisor_decision,
        repo_path=str(tmp_path),
        demo_mode=True,
    )
    assert isinstance(result, DeploymentResult)
    assert result.status == AgentStatus.DONE
    assert isinstance(result.log_lines, list)
    assert len(result.log_lines) > 0


@pytest.mark.asyncio
async def test_deployment_skip_for_test_only_changes(tmp_path, mock_supervisor_decision):
    from agents.deployment import run_deployment
    from schemas import DeployStrategy
    result = await run_deployment(
        changed_files=["tests/test_app.py"],
        supervisor_decision=mock_supervisor_decision,
        repo_path=str(tmp_path),
        demo_mode=True,
    )
    assert result.strategy == DeployStrategy.SKIP
